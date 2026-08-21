from __future__ import annotations

import copy
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from authority.aggregate_gate import verify_change
from authority.common import canonical_hash, sha256_bytes, sha256_file
from authority.conformance_gates import (
    _CHECKER_RUN_COLLECTORS,
    _CheckerRunObservation,
    audit_candidate_tree,
    audit_history_publication,
    run_validation_commands,
    verify_authority_references,
    verify_baseline_failure,
    verify_commit_tree,
    verify_independent_check,
    verify_reuse_admission,
    verify_runtime_predicate,
    verify_upstream_parity,
)
from authority.errors import GateViolation
from authority.lock import build_authority_lock
from authority.receipts import make_typed_receipt
from authority.task_gate import admit_task, check_change_scope, validate_task_manifest

REPO_ROOT = Path(__file__).parents[2]


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _fixture_repository(tmp_path: Path) -> tuple[Path, dict[str, Any], Path, Path, Path]:
    root = tmp_path / "task-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "governance").mkdir()
    task_dir = root / "task"
    task_dir.mkdir()
    for name in ("prd.md", "design.md", "implement.md", "implement.jsonl", "check.jsonl"):
        (task_dir / name).write_text(f"complete {name}\n", encoding="utf-8")
    model_policy = {
        "schema_version": "1.0.0",
        "risk_assignments": {
            "authority": {
                "allowed_model_families": ["gpt-5.6-sol"],
                "allowed_reasoning_classes": ["xhigh"],
            },
            "high": {
                "allowed_model_families": ["gpt-5.6-sol"],
                "allowed_reasoning_classes": ["xhigh"],
            },
        },
        "independence": {
            "different_run_identity_required": True,
            "different_context_hash_required": True,
            "protected_oracle_required": True,
        },
    }
    protected = {
        "schema_version": "1.0.0",
        "repositories": {"fixture": {"patterns": ["governance/**"]}},
    }
    authorizations = {
        "schema_version": "1.0.0",
        "authority_revision": 1,
        "authorizations": [
            {
                "authorization_id": "fixture-grant",
                "task_id": "authority-task",
                "task_type": "authority_change",
                "allowed_protected_paths": [{"repository": "fixture", "pattern": "governance/**"}],
                "approved_by": "fixture-user",
            }
        ],
    }
    model_path = root / "governance/model-role-policy.yaml"
    protected_path = root / "governance/protected-paths.yaml"
    auth_path = root / "governance/task-authorizations.yaml"
    model_path.write_text(yaml.safe_dump(model_policy), encoding="utf-8")
    protected_path.write_text(yaml.safe_dump(protected), encoding="utf-8")
    auth_path.write_text(yaml.safe_dump(authorizations), encoding="utf-8")
    (root / "governance/activation-profiles.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0.0",
                "profiles": {
                    "authority_bootstrap": {
                        "current_phase": "phase_minus_1",
                        "predicates": {
                            "registry_conformance": {"minimum_phase": "phase_0"},
                            "runtime_conformance": {"minimum_phase": "phase_4"},
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (root / "governance/registry.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0.0", "ids": ["SA-T-001"]}), encoding="utf-8"
    )
    (root / "governance/reuse-ledger.yaml").write_text(
        yaml.safe_dump({"schema_version": "1.0.0", "entries": []}), encoding="utf-8"
    )
    (root / "task/oracle.txt").write_text("protected oracle\n", encoding="utf-8")
    seed = _commit(root, "seed reviewed sources")
    sources = {
        "schema_version": "1.0.0",
        "authority_id": "fixture",
        "authority_revision": 1,
        "contract_version": "2.1.3",
        "seed_source_commit": seed,
        "repositories": {"fixture": {"source_commit": seed}},
        "entries": [
            {
                "class": "architecture_gate",
                "repository": "fixture",
                "path": "governance/task-authorizations.yaml",
            },
            {
                "class": "architecture_gate",
                "repository": "fixture",
                "path": "governance/model-role-policy.yaml",
            },
            {
                "class": "architecture_gate",
                "repository": "fixture",
                "path": "governance/protected-paths.yaml",
            },
            {
                "class": "architecture_gate",
                "repository": "fixture",
                "path": "governance/activation-profiles.yaml",
            },
            {
                "class": "blocking_fixture",
                "repository": "fixture",
                "path": "task/oracle.txt",
            },
            {
                "class": "registry_source",
                "repository": "fixture",
                "path": "governance/registry.yaml",
            },
            {
                "class": "registry_source",
                "repository": "fixture",
                "path": "governance/reuse-ledger.yaml",
            },
        ],
    }
    source_path = root / "governance/authority-sources.yaml"
    source_path.write_text(yaml.safe_dump(sources), encoding="utf-8")
    inventory = _commit(root, "freeze inventory")
    lock = build_authority_lock(
        source_manifest_repository="fixture",
        source_manifest_commit=inventory,
        source_manifest_path="governance/authority-sources.yaml",
        repository_roots={"fixture": root},
    )
    lock_path = root / "authority-lock.yaml"
    lock_path.write_text(yaml.safe_dump(lock), encoding="utf-8")
    return root, lock, lock_path, model_path, protected_path


def _context(root: Path, name: str) -> dict[str, Any]:
    path = root / f"task/{name}"
    return {
        "repository": "fixture",
        "path": f"task/{name}",
        "sha256": sha256_file(path),
        "byte_length": path.stat().st_size,
    }


def _manifest(
    root: Path, lock: dict[str, Any], *, task_type: str = "authority_change"
) -> dict[str, Any]:
    kinds = (
        ("prd", "prd.md"),
        ("design", "design.md"),
        ("implement", "implement.md"),
        ("implement_context", "implement.jsonl"),
        ("check_context", "check.jsonl"),
    )
    authority = task_type == "authority_change"
    return {
        "schema_version": "1.0.0",
        "task_id": "authority-task" if authority else "ordinary-task",
        "task_type": task_type,
        "risk_class": "authority" if authority else "high",
        "activation_profile": "authority_bootstrap",
        "authority_lock_hash": lock["bundle_hash"],
        "registry_binding": {
            "kind": "not_applicable",
            "profile": "authority_bootstrap",
            "minimum_phase": "phase_0",
            "current_phase": "phase_minus_1",
            "reason": "not activated",
            "authority_hash": lock["bundle_hash"],
        },
        "repository": "fixture",
        "branch": "main",
        "base_branch": "main",
        "worktree_path": str(root.resolve()),
        "predecessor_commit": _git(root, "rev-parse", "HEAD"),
        "repository_refs": {},
        "allowed_write_paths": [{"repository": "fixture", "pattern": "governance/**"}],
        "forbidden_runtime_import_roots": [{"repository": "fixture", "pattern": "legacy/**"}],
        "permitted_legacy_read_roots": [],
        "planning_artifacts": [{"kind": kind, **_context(root, name)} for kind, name in kinds],
        "implementation_context": _context(root, "implement.jsonl"),
        "check_context": _context(root, "check.jsonl"),
        "validation_commands": [{"command_id": "tests", "repository": "fixture", "argv": ["true"]}],
        "implementer": {
            "run_identity": "fixture:implementer",
            "model_family": "gpt-5.6-sol",
            "reasoning_class": "xhigh",
        },
        "checker_requirements": {
            "independent_run_required": True,
            "independent_context_required": True,
            "required_class": "authority",
        },
        "authority_change": (
            {
                "old_lock_hash": "sha256:" + "0" * 64,
                "new_lock_hash": lock["bundle_hash"],
                "compatibility_impact": "bootstrap",
                "invalidated_tasks": [],
            }
            if authority
            else None
        ),
    }


def _write_manifest(root: Path, manifest: dict[str, Any]) -> Path:
    path = root / "task-manifest.yaml"
    path.write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return path


def test_authority_task_requires_independent_locked_authorization(tmp_path: Path) -> None:
    root, lock, lock_path, model_path, protected_path = _fixture_repository(tmp_path)
    manifest = _manifest(root, lock)
    admit_task(
        manifest_path=_write_manifest(root, manifest),
        authority_lock_path=lock_path,
        model_policy_path=model_path,
        protected_paths_path=protected_path,
        repository_roots={"fixture": root},
    )
    manifest["task_id"] = "self-declared-but-ungranted"
    with pytest.raises(GateViolation, match="AUTH-TASK-AUTHORIZATION-MISSING"):
        admit_task(
            manifest_path=_write_manifest(root, manifest),
            authority_lock_path=lock_path,
            model_policy_path=model_path,
            protected_paths_path=protected_path,
            repository_roots={"fixture": root},
        )


def test_change_scope_is_derived_from_staged_index(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, protected_path = _fixture_repository(tmp_path)
    manifest = _manifest(root, lock)
    manifest_path = _write_manifest(root, manifest)
    (root / "governance/new-policy.yaml").write_text("closed: true\n", encoding="utf-8")
    _git(root, "add", "governance/new-policy.yaml")
    receipt = check_change_scope(
        manifest_path=manifest_path,
        protected_paths_path=protected_path,
        repository_roots={"fixture": root},
        authority_lock_hash=lock["bundle_hash"],
    )
    assert receipt["receipt_type"] == "change_scope"
    assert receipt["decision"] == "allow"


def test_ordinary_task_cannot_stage_protected_path(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, protected_path = _fixture_repository(tmp_path)
    manifest = _manifest(root, lock, task_type="implementation")
    manifest_path = _write_manifest(root, manifest)
    (root / "governance/new-policy.yaml").write_text("closed: true\n", encoding="utf-8")
    _git(root, "add", "governance/new-policy.yaml")
    with pytest.raises(GateViolation, match="AUTH-SCOPE-PROTECTED"):
        check_change_scope(
            manifest_path=manifest_path,
            protected_paths_path=protected_path,
            repository_roots={"fixture": root},
        )


def test_cross_repo_and_path_alias_validation_remain_fail_closed(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    manifest = _manifest(root, lock)
    manifest.update(
        {
            "repository": None,
            "branch": None,
            "base_branch": None,
            "worktree_path": None,
            "predecessor_commit": None,
        }
    )
    reference = {
        "branch": "main",
        "base_branch": "main",
        "worktree_path": str(root),
        "predecessor_commit": _git(root, "rev-parse", "HEAD"),
    }
    manifest["repository_refs"] = {"fixture": reference, "second": reference}
    manifest["repository"] = "fixture"
    with pytest.raises(GateViolation, match="AUTH-TASK-CROSS-REPO-TOPLEVEL"):
        validate_task_manifest(manifest)
    alias = _manifest(root, lock)
    alias["allowed_write_paths"] = [{"repository": "fixture", "pattern": "governance//**"}]
    with pytest.raises(GateViolation, match="AUTH-PATH-ALIAS"):
        validate_task_manifest(alias)


def test_manifest_validation_does_not_mutate_input(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    manifest = _manifest(root, lock)
    original = copy.deepcopy(manifest)
    validate_task_manifest(manifest)
    assert manifest == original


def test_candidate_commit_reference_reuse_and_validation_gates(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    predecessor = _git(root, "rev-parse", "HEAD")
    (root / "safe.py").write_text("import json\nRULE = 'SA-T-001'\n", encoding="utf-8")
    _git(root, "add", "safe.py")
    candidate = audit_candidate_tree(
        root=root,
        predecessor_commit=predecessor,
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
    )
    reuse = verify_reuse_admission(
        root=root,
        predecessor_commit=predecessor,
        task_id="task",
        authority_lock=lock,
        repository_roots={"fixture": root},
        ledger_path="governance/reuse-ledger.yaml",
    )
    reference = verify_authority_references(
        root=root,
        predecessor_commit=predecessor,
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
        authority_lock=lock,
        repository_roots={"fixture": root},
        registry_paths=["governance/registry.yaml"],
    )
    validation = run_validation_commands(
        root=root,
        predecessor_commit=predecessor,
        repository="fixture",
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
        environment={"python": "3.13"},
        command_specs=[
            {
                "command_id": "pytest",
                "repository": "fixture",
                "argv": ["python", "-c", "import sys; sys.exit(0)"],
            }
        ],
    )
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        "candidate",
    )
    committed = _git(root, "rev-parse", "HEAD")
    commit_receipt = verify_commit_tree(
        root=root,
        candidate_commit=committed,
        approved_tree=candidate["staged_tree_hash"],
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
    )
    assert {
        reuse["decision"],
        reference["decision"],
        validation["decision"],
        commit_receipt["decision"],
    } == {"allow"}


def test_legacy_import_and_unresolved_reference_deny(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    predecessor = _git(root, "rev-parse", "HEAD")
    legacy = root / "packages/autocut-kernel/kernel.py"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("import autocut_core\n", encoding="utf-8")
    _git(root, "add", str(legacy.relative_to(root)))
    with pytest.raises(GateViolation, match="AUTH-REUSE-DENY"):
        verify_reuse_admission(
            root=root,
            predecessor_commit=predecessor,
            task_id="task",
            authority_lock=lock,
            repository_roots={"fixture": root},
            ledger_path="governance/reuse-ledger.yaml",
        )
    (root / "unknown-rule.md").write_text("must satisfy SA-T-999\n", encoding="utf-8")
    _git(root, "add", "unknown-rule.md")
    with pytest.raises(GateViolation, match="AUTH-REFERENCE-UNRESOLVED"):
        verify_authority_references(
            root=root,
            predecessor_commit=predecessor,
            task_id="task",
            authority_lock_hash=lock["bundle_hash"],
            authority_lock=lock,
            repository_roots={"fixture": root},
            registry_paths=["governance/registry.yaml"],
        )


def test_profile_na_independence_upstream_and_baseline_gates(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    receipt = verify_runtime_predicate(
        task_id="task",
        authority_lock=lock,
        repository_roots={"fixture": root},
        profile="authority_bootstrap",
        predicate_id="runtime_conformance",
        observed_pass=None,
    )
    assert receipt["decision"] == "not_applicable" and receipt["profile_signature"].startswith(
        "sha256:"
    )
    digest3 = "sha256:" + "3" * 64
    oracle_hash = next(
        entry["sha256"] for entry in lock["entries"] if entry["class"] == "blocking_fixture"
    )
    task_manifest = _manifest(root, lock)
    task_manifest["task_id"] = "task"
    tree_oid = _git(root, "write-tree")
    checker_input_hash = canonical_hash(
        {
            "task_id": task_manifest["task_id"],
            "authority_lock_hash": lock["bundle_hash"],
            "candidate_tree_hash": tree_oid,
            "check_context_hash": task_manifest["check_context"]["sha256"],
            "checker_requirements": task_manifest["checker_requirements"],
        }
    )
    checker_payload = {
        "task_id": "task",
        "implementer_run_identity": "fixture:implementer",
        "checker_run_identity": "run-b",
        "implementation_context_hash": task_manifest["implementation_context"]["sha256"],
        "check_context_hash": task_manifest["check_context"]["sha256"],
        "candidate_tree_hash": tree_oid,
        "checker_input_manifest_hash": checker_input_hash,
        "protected_oracle_hashes": [oracle_hash],
        "command_results_hash": digest3,
        "conclusion": "pass",
    }
    _CHECKER_RUN_COLLECTORS["fixture-checker"] = lambda _root, _task: _CheckerRunObservation(
        normalized=checker_payload, raw_evidence=b"fixture protected checker evidence"
    )
    independent = verify_independent_check(
        authority_lock=lock,
        repository_roots={"fixture": root},
        task_manifest=task_manifest,
        root=root,
        checker_collector_id="fixture-checker",
    )
    oid = _commit(root, "freeze generated fixture lock")
    (root / "upstream-feature.txt").write_text("upstream feature\n", encoding="utf-8")
    upstream_head = _commit(root, "upstream feature")
    mapping = {
        "schema_version": "1.0.0",
        "upstream_base": oid,
        "upstream_head": upstream_head,
        "mappings": [
            {
                "capability_id": "path:upstream-feature.txt",
                "disposition": "preserved",
                "evidence": ["task/prd.md"],
            }
        ],
    }
    (root / "upstream-mapping.yaml").write_text(yaml.safe_dump(mapping), encoding="utf-8")
    local_candidate = _commit(root, "map upstream capability")
    upstream = verify_upstream_parity(
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
        root=root,
        protected_patterns=["governance/**"],
        manifest={
            "upstream_base": oid,
            "upstream_head": upstream_head,
            "local_base": upstream_head,
            "local_candidate": local_candidate,
            "mapping_registry_path": "upstream-mapping.yaml",
        },
    )
    baseline = verify_baseline_failure(
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
        root=root,
        manifest={
            "baseline_commit": local_candidate,
            "candidate_commit": local_candidate,
            "argv": ["python", "-c", "import sys; print('known'); sys.exit(3)"],
            "environment": {},
            "classification_owner": "owner",
        },
    )
    assert independent["decision"] == upstream["decision"] == baseline["decision"] == "allow"


def test_history_gate_scans_deleted_historical_blob(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    base = _git(root, "rev-parse", "HEAD")
    leaked_path = root / "notes.txt"
    leaked_path.write_text("sk-" + "outgoinghistoryfixture000000000000\n", encoding="utf-8")
    leaked = _commit(root, "leak")
    leaked_path.unlink()
    candidate = _commit(root, "delete leak")
    remote = make_typed_receipt(
        "remote_protection",
        authority_lock_hash=lock["bundle_hash"],
        decision="allow",
        reason_codes=[],
        task_id="task",
        remote_canonical_url="https://example.test/repo",
        target_ref="refs/heads/main",
        expected_remote_oid=base,
        candidate_commit=candidate,
        policy_hash="sha256:" + "1" * 64,
        collector_evidence_hash="sha256:" + "2" * 64,
        collector_id="fixture-live-v1",
        protection_enabled=True,
        required_checks_hash="sha256:" + "3" * 64,
        fetched_at="2026-08-21T00:00:00.000Z",
        expires_at="2026-08-21T00:05:00.000Z",
    )
    assert leaked
    with pytest.raises(GateViolation, match="AUTH-HISTORY-UNSAFE"):
        audit_history_publication(
            root=root,
            task_id="task",
            authority_lock_hash=lock["bundle_hash"],
            candidate_commit=candidate,
            remote_attestation=remote,
        )


def test_candidate_audit_scans_full_tree_for_nfc_casefold_collisions(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    predecessor = _git(root, "rev-parse", "HEAD")
    first = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=root,
        input="one\n",
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()
    second = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=root,
        input="two\n",
        text=True,
        check=True,
        capture_output=True,
    ).stdout.strip()
    _git(root, "update-index", "--add", "--cacheinfo", "100644", first, "Docs/A.md")
    _git(root, "update-index", "--add", "--cacheinfo", "100644", second, "docs/a.md")
    with pytest.raises(GateViolation, match="AUTH-CANDIDATE-UNSAFE.*path_alias"):
        audit_candidate_tree(
            root=root,
            predecessor_commit=predecessor,
            task_id="task",
            authority_lock_hash=lock["bundle_hash"],
        )


def test_candidate_audit_rejects_privacy_content_in_unchanged_blob(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    private = root / "runtime/sessions/ws.json"
    private.parent.mkdir(parents=True)
    private.write_text("runtime transcript\n", encoding="utf-8")
    predecessor = _commit(root, "accidentally track runtime session")
    (root / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "safe.py")
    with pytest.raises(GateViolation, match="AUTH-CANDIDATE-UNSAFE.*sensitive_path"):
        audit_candidate_tree(
            root=root,
            predecessor_commit=predecessor,
            task_id="task",
            authority_lock_hash=lock["bundle_hash"],
        )


def test_candidate_audit_allows_legal_session_modules_and_variable_flow(tmp_path: Path) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    predecessor = _git(root, "rev-parse", "HEAD")
    legal = root / "tests/session/test_api.py"
    legal.parent.mkdir(parents=True)
    legal.write_text(
        "def use(api_key_from_env):\n    return send(api_key_from_env)\n",
        encoding="utf-8",
    )
    _git(root, "add", "tests/session/test_api.py")
    receipt = audit_candidate_tree(
        root=root,
        predecessor_commit=predecessor,
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
    )
    assert receipt["decision"] == "allow"


def test_synthetic_secret_requires_exact_manifest_hash_path_marker_and_profile(
    tmp_path: Path,
) -> None:
    root, lock, _lock_path, _model_path, _protected_path = _fixture_repository(tmp_path)
    predecessor = _git(root, "rev-parse", "HEAD")
    fixture_path = "tests/authority/fixtures/synthetic-sensitive/literal.txt"
    fixture = root / fixture_path
    fixture.parent.mkdir(parents=True)
    raw = b"AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1\nsk-" + b"syntheticfixture0000000000000000\n"
    fixture.write_bytes(raw)
    _git(root, "add", fixture_path)
    manifest = {
        "schema_version": "1.0.0",
        "allowed_root": "tests/authority/fixtures/synthetic-sensitive",
        "marker": "AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1",
        "fixtures": [
            {
                "fixture_id": "AUTH-SYNTHETIC-TEST-001",
                "path": fixture_path,
                "sha256": sha256_bytes(raw),
                "profile": "test_fixture",
            }
        ],
    }
    allowed = audit_candidate_tree(
        root=root,
        predecessor_commit=predecessor,
        task_id="task",
        authority_lock_hash=lock["bundle_hash"],
        scan_profile="test_fixture",
        synthetic_fixture_manifest=manifest,
    )
    assert allowed["decision"] == "allow"
    with pytest.raises(GateViolation, match="AUTH-CANDIDATE-UNSAFE"):
        audit_candidate_tree(
            root=root,
            predecessor_commit=predecessor,
            task_id="task",
            authority_lock_hash=lock["bundle_hash"],
            scan_profile="production",
            synthetic_fixture_manifest=manifest,
        )


def test_aggregate_change_gate_recomputes_and_closes_all_receipts(tmp_path: Path) -> None:
    root, lock, lock_path, model_path, protected_path = _fixture_repository(tmp_path)
    manifest = _manifest(root, lock)
    manifest["validation_commands"] = [
        {"command_id": "fixture", "repository": "fixture", "argv": ["true"]}
    ]
    manifest_path = _write_manifest(root, manifest)
    (root / "governance/new-policy.yaml").write_text("closed: true\n", encoding="utf-8")
    _git(root, "add", "governance/new-policy.yaml")
    tree_oid = _git(root, "write-tree")
    oracle_hash = next(
        entry["sha256"] for entry in lock["entries"] if entry["class"] == "blocking_fixture"
    )
    checker_input_hash = canonical_hash(
        {
            "task_id": manifest["task_id"],
            "authority_lock_hash": lock["bundle_hash"],
            "candidate_tree_hash": tree_oid,
            "check_context_hash": manifest["check_context"]["sha256"],
            "checker_requirements": manifest["checker_requirements"],
        }
    )

    def aggregate_checker(check_root: Path, _task: dict[str, Any]) -> _CheckerRunObservation:
        validation = run_validation_commands(
            root=check_root,
            predecessor_commit=manifest["predecessor_commit"],
            repository="fixture",
            task_id=manifest["task_id"],
            authority_lock_hash=lock["bundle_hash"],
            command_specs=manifest["validation_commands"],
        )
        payload = {
            "task_id": manifest["task_id"],
            "implementer_run_identity": manifest["implementer"]["run_identity"],
            "checker_run_identity": "fixture:checker",
            "implementation_context_hash": manifest["implementation_context"]["sha256"],
            "check_context_hash": manifest["check_context"]["sha256"],
            "candidate_tree_hash": tree_oid,
            "checker_input_manifest_hash": checker_input_hash,
            "protected_oracle_hashes": [oracle_hash],
            "command_results_hash": validation["command_results_hash"],
            "conclusion": "pass",
        }
        return _CheckerRunObservation(
            normalized=payload, raw_evidence=b"protected aggregate checker run"
        )

    _CHECKER_RUN_COLLECTORS["fixture-aggregate-checker"] = aggregate_checker
    bundle = verify_change(
        task_manifest_path=manifest_path,
        authority_lock_path=lock_path,
        model_policy_path=model_path,
        protected_paths_path=protected_path,
        repository_roots={"fixture": root},
        registry_paths=["governance/registry.yaml"],
        reuse_ledger_path="governance/reuse-ledger.yaml",
        checker_collector_ids={"fixture": "fixture-aggregate-checker"},
    )
    assert bundle["receipt"]["decision"] == "allow"
    assert bundle["repository_trees"] == [
        {
            "repository": "fixture",
            "base_commit": manifest["predecessor_commit"],
            "tree_oid": tree_oid,
        }
    ]
    good_collector = _CHECKER_RUN_COLLECTORS["fixture-aggregate-checker"]

    def mismatched_checker(check_root: Path, task: dict[str, Any]) -> _CheckerRunObservation:
        observed = good_collector(check_root, task)
        payload = dict(observed.normalized)
        payload["command_results_hash"] = "sha256:" + "9" * 64
        return _CheckerRunObservation(normalized=payload, raw_evidence=observed.raw_evidence)

    _CHECKER_RUN_COLLECTORS["fixture-aggregate-checker"] = mismatched_checker
    with pytest.raises(GateViolation, match="AUTH-AGGREGATE-CHECKER-VALIDATION"):
        verify_change(
            task_manifest_path=manifest_path,
            authority_lock_path=lock_path,
            model_policy_path=model_path,
            protected_paths_path=protected_path,
            repository_roots={"fixture": root},
            registry_paths=["governance/registry.yaml"],
            reuse_ledger_path="governance/reuse-ledger.yaml",
            checker_collector_ids={"fixture": "fixture-aggregate-checker"},
        )
