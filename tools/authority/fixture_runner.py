# pyright: reportUnknownArgumentType=false, reportUnknownVariableType=false
"""Independent executor for protected blocking fixtures."""

from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import yaml

from .common import (
    canonical_hash,
    load_mapping,
    require_closed,
    require_git_object_oid,
    require_list,
    sha256_bytes,
    sha256_file,
)
from .conformance_gates import audit_candidate_tree, verify_validation_receipt_set
from .consumer_lock import (
    AUTHORITY_SYNC_TASK_ID,
    CONSUMER_LOCK_PATH,
    KERNEL_BUILD_EVIDENCE_POLICY_PATH,
    assert_phase00_consumer_lock_absent,
    authorize_consumer_lock_use,
    build_authority_consumer_lock,
    compute_kernel_source_subtree_hash,
    validate_authority_consumer_lock,
    validate_authority_consumer_lock_structure,
    verify_kernel_build_evidence,
)
from .errors import GateViolation
from .lock import verify_bootstrap_commit_chain
from .receipts import make_typed_receipt
from .remote_gate import reject_offline_remote_evidence
from .source_candidate_gate import verify_pre_a_source_candidate
from .task_gate import (
    validate_context_content,
    validate_model_role,
    validate_task_activation_profile,
    validate_task_manifest,
)


def _base_manifest() -> dict[str, Any]:
    zero = "0" * 40
    digest = "sha256:" + "0" * 64
    context = {
        "repository": "auto_cut_bot",
        "path": "task/prd.md",
        "sha256": digest,
        "byte_length": 1,
    }
    return {
        "schema_version": "1.0.0",
        "task_id": "protected-fixture",
        "task_type": "authority_change",
        "risk_class": "authority",
        "activation_profile": "authority_bootstrap",
        "authority_lock_hash": digest,
        "registry_binding": {
            "kind": "not_applicable",
            "profile": "authority_bootstrap",
            "minimum_phase": "phase_0",
            "current_phase": "phase_minus_1",
            "reason": "not activated",
            "authority_hash": digest,
        },
        "repository": None,
        "branch": None,
        "base_branch": None,
        "worktree_path": None,
        "predecessor_commit": None,
        "repository_refs": {
            "auto_cut_bot": {
                "branch": "fixture",
                "base_branch": "main",
                "worktree_path": "/tmp/fixture-a",
                "predecessor_commit": zero,
            },
            "ac_auto_cut": {
                "branch": "fixture",
                "base_branch": "main",
                "worktree_path": "/tmp/fixture-b",
                "predecessor_commit": zero,
            },
        },
        "allowed_write_paths": [{"repository": "auto_cut_bot", "pattern": "governance/**"}],
        "forbidden_runtime_import_roots": [
            {"repository": "ac_auto_cut", "pattern": "autocut_core/**"}
        ],
        "permitted_legacy_read_roots": [],
        "planning_artifacts": [
            {"kind": kind, **context}
            for kind in ("prd", "design", "implement", "implement_context", "check_context")
        ],
        "implementation_context": context,
        "check_context": {**context, "path": "task/check.md"},
        "validation_commands": [
            {"command_id": "fixture", "repository": "auto_cut_bot", "argv": ["true"]}
        ],
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
        "authority_change": {
            "old_lock_hash": digest,
            "new_lock_hash": digest,
            "compatibility_impact": "fixture",
            "invalidated_tasks": [],
        },
    }


def _set_dotted(value: dict[str, Any], field: str, replacement: Any) -> None:
    target: Any = value
    parts = field.replace("]", "").replace("[", ".").split(".")
    for part in parts[:-1]:
        target = target[int(part)] if part.isdigit() else target[part]
    final = parts[-1]
    if final.isdigit():
        target[int(final)] = replacement
    else:
        target[final] = replacement


def _execute_fixture(runner_id: str, path: Path, model_policy: dict[str, Any]) -> None:
    if runner_id == "context_content":
        validate_context_content(path.read_bytes(), where=str(path))
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    digest = "sha256:" + "0" * 64
    if runner_id == "receipt_decision":
        make_typed_receipt(
            "task_admission",
            authority_lock_hash=digest,
            decision="not_applicable",
            reason_codes=[],
            task_id="fixture",
            context_hash=digest,
            repository_heads_hash=digest,
            authorization_id=None,
        )
        return
    if runner_id == "offline_remote_evidence":
        reject_offline_remote_evidence(path)
        return
    if runner_id == "validation_self_report":
        verify_validation_receipt_set(
            task_id="fixture",
            authority_lock_hash=digest,
            staged_tree_hash="0" * 40,
            environment={},
            command_results=[],
        )
        return
    if runner_id == "consumer_lock_negative":
        _execute_consumer_lock_fixture(payload, path.parents[4])
        return
    if runner_id in {
        "candidate_secret",
        "candidate_collision",
        "candidate_legal_session",
        "candidate_synthetic_profile",
        "candidate_synthetic_outside",
        "fake_tree_oid",
        "bootstrap_chain",
        "pre_a_phase_mix",
    }:
        _execute_git_fixture(runner_id, payload)
        return
    mutation = payload["mutation"]
    manifest = copy.deepcopy(_base_manifest())
    _set_dotted(manifest, mutation["field"], mutation["value"])
    if runner_id == "task_manifest_mutation":
        validate_task_manifest(manifest)
    elif runner_id == "model_role_mutation":
        validate_model_role(manifest, model_policy)
    else:
        raise GateViolation("AUTH-FIXTURE-RUNNER", f"unknown runner: {runner_id}")


def _wheel_record_hash(raw: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
    return f"sha256={digest}"


def _write_fixture_wheel(path: Path, *, package_bytes: bytes = b"VALUE = 2\n") -> None:
    dist_info = "autocut_kernel-2.1.3.dev1.dist-info"
    files = {
        "autocut_kernel/__init__.py": package_bytes,
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: protected-fixture\nRoot-Is-Purelib: true\n"
            b"Tag: py3-none-any\n"
        ),
        f"{dist_info}/METADATA": (
            b"Metadata-Version: 2.1\nName: autocut-kernel\nVersion: 2.1.3.dev1\n"
        ),
    }
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, raw in files.items():
        writer.writerow((name, _wheel_record_hash(raw), str(len(raw))))
    record_path = f"{dist_info}/RECORD"
    writer.writerow((record_path, "", ""))
    files[record_path] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)


def _consumer_lock_fixture_values(
    repository_root: Path, scratch: Path
) -> tuple[dict[str, Any], object, dict[str, Any], dict[str, Path | str]]:
    policy = load_mapping(repository_root / "governance/consumer-lock-policy.yaml")
    root = scratch / "authority"
    root.mkdir()
    _git(root, "init", "-b", "main")
    policy_path = root / KERNEL_BUILD_EVIDENCE_POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    policy_raw = (repository_root / KERNEL_BUILD_EVIDENCE_POLICY_PATH).read_bytes()
    policy_path.write_bytes(policy_raw)
    kernel_file = root / "packages/autocut-kernel/src/autocut_kernel/__init__.py"
    kernel_file.parent.mkdir(parents=True)
    kernel_file.write_text("VALUE = 1\n", encoding="utf-8")
    marker = root / "authority-source.txt"
    marker.write_text("authority source\n", encoding="utf-8")
    seed = _fixture_commit(root, "authority source")
    authority_lock: dict[str, Any] = {
        "schema_version": "1.0.0",
        "authority_id": "fixture-authority",
        "authority_revision": 3,
        "contract_version": "2.1.3",
        "seed_source_commit": seed,
        "inventory": {
            "repository": "auto_cut_bot",
            "manifest_commit": seed,
            "path": "authority-source.txt",
            "sha256": sha256_bytes(marker.read_bytes()),
        },
        "repositories": {"auto_cut_bot": {"source_commit": seed}},
        "entries": [
            {
                "class": "architecture_gate",
                "repository": "auto_cut_bot",
                "path": KERNEL_BUILD_EVIDENCE_POLICY_PATH,
                "sha256": sha256_bytes(policy_raw),
            }
        ],
    }
    authority_lock["bundle_hash"] = canonical_hash(authority_lock)
    authority_lock_path = root / "governance/authority-lock.yaml"
    authority_lock_path.write_text(
        yaml.safe_dump(authority_lock, sort_keys=False), encoding="utf-8"
    )
    authority_commit = _fixture_commit(root, "authority lock")
    kernel_file.write_text("VALUE = 2\n", encoding="utf-8")
    kernel_commit = _fixture_commit(root, "kernel source")

    evidence_root = scratch / "evidence"
    evidence_root.mkdir()
    wheel_path = evidence_root / "autocut_kernel-2.1.3.dev1-py3-none-any.whl"
    _write_fixture_wheel(wheel_path)
    recipe_path = evidence_root / "build-recipe.json"
    recipe_path.write_text('{"command":["python","-m","build"]}\n', encoding="utf-8")
    environment_path = evidence_root / "environment.lock"
    environment_path.write_text("python==3.11.13\nbuild==1.3.0\n", encoding="utf-8")
    provenance = {
        "schema_version": "1.0.0",
        "receipt_type": "kernel_build_provenance",
        "builder_id": "autocut-isolated-wheel-builder-v1",
        "decision": "allow",
        "build_evidence_policy_hash": sha256_bytes(policy_raw),
        "kernel_source_commit": kernel_commit,
        "kernel_source_subtree_hash": compute_kernel_source_subtree_hash(root, kernel_commit),
        "wheel_sha256": sha256_bytes(wheel_path.read_bytes()),
        "build_recipe_hash": sha256_bytes(recipe_path.read_bytes()),
        "environment_lock_hash": sha256_bytes(environment_path.read_bytes()),
    }
    provenance_path = evidence_root / "provenance.json"
    provenance_path.write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
    evidence = verify_kernel_build_evidence(
        authority_repository_root=root,
        authority_governance_commit=authority_commit,
        kernel_repository_root=root,
        kernel_source_commit=kernel_commit,
        wheel_path=wheel_path,
        distribution_version="2.1.3.dev1",
        build_recipe_path=recipe_path,
        environment_lock_path=environment_path,
        provenance_receipt_path=provenance_path,
    )
    lock = build_authority_consumer_lock(
        kernel_build_evidence=evidence,
        eligibility_profile="bootstrap_consumable",
        profile_policy=policy,
    )
    paths: dict[str, Path | str] = {
        "root": root,
        "authority_commit": authority_commit,
        "kernel_commit": kernel_commit,
        "wheel": wheel_path,
        "recipe": recipe_path,
        "environment": environment_path,
        "provenance": provenance_path,
    }
    return policy, evidence, lock, paths


def _execute_consumer_lock_fixture(payload: dict[str, Any], repository_root: Path) -> None:
    case = payload.get("case")
    with tempfile.TemporaryDirectory(prefix="consumer-lock-protected-") as raw_root:
        scratch = Path(raw_root)
        policy, evidence, lock, paths = _consumer_lock_fixture_values(repository_root, scratch)
        if case in {
            "phase00_materialized",
            "commit_worktree_divergence",
            "index_worktree_divergence",
        }:
            root = scratch / "consumer"
            root.mkdir()
            _git(root, "init", "-b", "main")
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            seed = _fixture_commit(root, "consumer seed")
            target = root / CONSUMER_LOCK_PATH
            target.parent.mkdir(parents=True)
            target.write_text("forbidden: true\n", encoding="utf-8")
            commit = seed
            if case == "commit_worktree_divergence":
                commit = _fixture_commit(root, "commit forbidden lock")
                target.unlink()
            elif case == "index_worktree_divergence":
                _git(root, "add", CONSUMER_LOCK_PATH)
                _git(root, "update-index", "--skip-worktree", CONSUMER_LOCK_PATH)
                target.unlink()
            assert_phase00_consumer_lock_absent(
                consumer_repository_root=root, consumer_repository_commit=commit
            )
            return
        if case == "reserved_path_override":
            make_typed_receipt(
                "consumer_lock_readiness",
                authority_lock_hash="sha256:" + "1" * 64,
                decision="not_applicable",
                reason_codes=[],
                task_id=AUTHORITY_SYNC_TASK_ID,
                authority_governance_commit="a" * 40,
                authority_bundle_hash="sha256:" + "1" * 64,
                consumer_repository_commit="b" * 40,
                consumer_commit_tree_oid="c" * 40,
                consumer_index_tree_oid="d" * 40,
                consumer_lock_path="governance/caller-selected.lock.yaml",
                state="not_materialized",
                reason="kernel_build_not_yet_available",
                profile_policy_hash=canonical_hash(policy),
            )
            return
        if case == "fabricated_build_receipt":
            build_authority_consumer_lock(
                kernel_build_evidence={"receipt_type": "kernel_build"},  # type: ignore[arg-type]
                eligibility_profile="bootstrap_consumable",
                profile_policy=policy,
            )
            return
        if case in {"altered_wheel", "altered_build_recipe"}:
            changed = Path(paths["wheel"] if case == "altered_wheel" else paths["recipe"])
            if case == "altered_wheel":
                _write_fixture_wheel(changed, package_bytes=b"VALUE = 999\n")
            else:
                changed.write_bytes(changed.read_bytes() + b"altered\n")
            build_authority_consumer_lock(
                kernel_build_evidence=evidence,
                eligibility_profile="bootstrap_consumable",
                profile_policy=policy,
            )
            return
        if case in {
            "pending_placeholder",
            "authority_hash_mismatch",
            "kernel_source_hash_mismatch",
            "wheel_hash_mismatch",
            "self_reference",
        }:
            mutated = copy.deepcopy(lock)
            if case == "pending_placeholder":
                mutated["distribution_version"] = str(payload.get("value", "pending"))
            elif case == "authority_hash_mismatch":
                mutated["authority_bundle_hash"] = "sha256:" + "8" * 64
            elif case == "kernel_source_hash_mismatch":
                mutated["kernel_source_subtree_hash"] = "sha256:" + "8" * 64
            elif case == "wheel_hash_mismatch":
                mutated["wheel_sha256"] = "sha256:" + "8" * 64
            else:
                mutated["consumer_repository_commit"] = "c" * 40
            validate_authority_consumer_lock_structure(mutated, profile_policy=policy)
            if case not in {"pending_placeholder", "self_reference"}:
                validate_authority_consumer_lock(
                    mutated, profile_policy=policy, kernel_build_evidence=evidence
                )
            return
        if case in {
            "bootstrap_writer",
            "bootstrap_business",
            "bootstrap_shadow",
            "bootstrap_publish",
        }:
            capability = {
                "bootstrap_writer": "authority_store_write",
                "bootstrap_business": "business_execution",
                "bootstrap_shadow": "shadow_output",
                "bootstrap_publish": "publication",
            }[str(case)]
            authorize_consumer_lock_use(
                lock=lock,
                profile_policy=policy,
                requested_capabilities=[capability],
                verified_post_commit_receipts=[],
                kernel_build_evidence=evidence,
            )
            return
        if case in {"child02_wrong_profile_na", "stale_task_authorization_revision"}:
            task_policy = load_mapping(repository_root / "governance/task-authorizations.yaml")
            if case == "stale_task_authorization_revision":
                task_policy["authority_revision"] = 2
            validate_task_activation_profile(
                task_id="08-21-02-import-firewall-and-package-skeleton",
                activation_profile=(
                    "authority_bootstrap"
                    if case == "child02_wrong_profile_na"
                    else "authority_package_skeleton"
                ),
                task_authorizations=task_policy,
                authority_revision=3,
            )
            return
    raise GateViolation("AUTH-FIXTURE-RUNNER", f"unknown consumer lock case: {case}")


def _git(root: Path, *args: str, input_text: str | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
        input=input_text,
    ).stdout.strip()


def _fixture_commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _execute_git_fixture(runner_id: str, payload: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory(prefix="authority-protected-fixture-") as raw_root:
        root = Path(raw_root)
        _git(root, "init", "-b", "main")
        (root / "seed.txt").write_text("seed\n", encoding="utf-8")
        seed = _fixture_commit(root, "seed")
        if runner_id == "candidate_secret":
            secret = "".join(payload["segments"])
            (root / "candidate.txt").write_text(secret + "\n", encoding="utf-8")
            _git(root, "add", "candidate.txt")
            audit_candidate_tree(
                root=root,
                predecessor_commit=seed,
                task_id="fixture",
                authority_lock_hash="sha256:" + "0" * 64,
            )
            return
        if runner_id == "candidate_collision":
            for index, candidate_path in enumerate(payload["paths"]):
                oid = _git(root, "hash-object", "-w", "--stdin", input_text=f"{index}\n")
                _git(root, "update-index", "--add", "--cacheinfo", "100644", oid, candidate_path)
            audit_candidate_tree(
                root=root,
                predecessor_commit=seed,
                task_id="fixture",
                authority_lock_hash="sha256:" + "0" * 64,
            )
            return
        if runner_id == "candidate_legal_session":
            candidate_path = str(payload["path"])
            target = root / candidate_path
            target.parent.mkdir(parents=True)
            target.write_text(
                "def use(api_key_from_env):\n    return send(api_key_from_env)\n",
                encoding="utf-8",
            )
            _git(root, "add", candidate_path)
            audit_candidate_tree(
                root=root,
                predecessor_commit=seed,
                task_id="fixture",
                authority_lock_hash="sha256:" + "0" * 64,
            )
            return
        if runner_id in {"candidate_synthetic_profile", "candidate_synthetic_outside"}:
            candidate_path = str(
                payload.get("path", "tests/authority/fixtures/synthetic-sensitive/literal.txt")
            )
            target = root / candidate_path
            target.parent.mkdir(parents=True)
            raw = (
                b"AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1\nsk-" + b"protectedfixture0000000000000000\n"
            )
            target.write_bytes(raw)
            _git(root, "add", candidate_path)
            synthetic_manifest = {
                "schema_version": "1.0.0",
                "allowed_root": "tests/authority/fixtures/synthetic-sensitive",
                "marker": "AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1",
                "fixtures": [
                    {
                        "fixture_id": "AUTH-SYNTHETIC-PROTECTED-001",
                        "path": candidate_path,
                        "sha256": sha256_bytes(raw),
                        "profile": "test_fixture",
                    }
                ],
            }
            audit_candidate_tree(
                root=root,
                predecessor_commit=seed,
                task_id="fixture",
                authority_lock_hash="sha256:" + "0" * 64,
                scan_profile=str(payload.get("profile", "test_fixture")),
                synthetic_fixture_manifest=synthetic_manifest,
            )
            return
        if runner_id == "fake_tree_oid":
            require_git_object_oid(root, payload["oid"], object_type="tree", where="fixture oid")
            return
        if runner_id == "pre_a_phase_mix":
            fixture_path = "tests/authority/fixtures/synthetic-sensitive/literal.txt"
            fixture = root / fixture_path
            fixture.parent.mkdir(parents=True)
            raw = b"AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1\nsk-" + b"preafixure00000000000000000000\n"
            fixture.write_bytes(raw)
            manifest_path = "governance/synthetic-sensitive-fixtures.manifest.yaml"
            manifest_file = root / manifest_path
            manifest_file.parent.mkdir(parents=True)
            manifest_file.write_text(
                yaml.safe_dump(
                    {
                        "schema_version": "1.0.0",
                        "allowed_root": "tests/authority/fixtures/synthetic-sensitive",
                        "marker": "AUTOCUT_SYNTHETIC_SECRET_FIXTURE_V1",
                        "fixtures": [
                            {
                                "fixture_id": "AUTH-SYNTHETIC-PRE-A-001",
                                "path": fixture_path,
                                "sha256": sha256_bytes(raw),
                                "profile": "test_fixture",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            forbidden = root / str(payload["path"])
            forbidden.write_text("schema_version: 1.0.0\n", encoding="utf-8")
            _git(root, "add", "governance", "tests/authority")
            verify_pre_a_source_candidate(
                root=root,
                predecessor_commit=seed,
                synthetic_fixture_manifest_path=manifest_path,
            )
            return
        inventory = root / "authority-sources.yaml"
        inventory.write_text("schema_version: 1.0.0\n", encoding="utf-8")
        (root / "unexpected.txt").write_text("not inventory\n", encoding="utf-8")
        inventory_commit = _fixture_commit(root, "bad inventory")
        (root / "authority-lock.yaml").write_text("schema_version: 1.0.0\n", encoding="utf-8")
        lock_commit = _fixture_commit(root, "lock")
        verify_bootstrap_commit_chain(
            repository_root=root,
            seed_commit=seed,
            inventory_commit=inventory_commit,
            lock_commit=lock_commit,
            source_manifest_repository="fixture",
            source_manifest_path="authority-sources.yaml",
            generated_lock_path="authority-lock.yaml",
            repository_roots={"fixture": root},
        )


def run_blocking_fixtures(
    *, manifest_path: Path, repository_root: Path, model_policy_path: Path
) -> list[str]:
    manifest = load_mapping(manifest_path)
    require_closed(
        manifest,
        required=("schema_version", "activation_profile", "fixtures"),
        where="blocking fixture manifest",
    )
    model_policy = load_mapping(model_policy_path)
    executed: list[str] = []
    for index, item in enumerate(
        require_list(manifest["fixtures"], where="fixtures", non_empty=True)
    ):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-FIXTURE-MANIFEST", f"fixture {index} invalid")
        require_closed(
            item,
            required=(
                "fixture_id",
                "runner_id",
                "path",
                "sha256",
                "expected_decision",
                "expected_reason_code",
            ),
            where=f"fixture {index}",
        )
        path = repository_root / item["path"]
        if sha256_file(path) != item["sha256"]:
            raise GateViolation("AUTH-FIXTURE-HASH", f"fixture hash mismatch: {item['fixture_id']}")
        observed = "allow"
        reason: str | None = None
        try:
            _execute_fixture(str(item["runner_id"]), path, model_policy)
        except GateViolation as exc:
            observed, reason = "deny", exc.code
        if observed != item["expected_decision"] or reason != item["expected_reason_code"]:
            raise GateViolation(
                "AUTH-FIXTURE-EXPECTATION",
                f"{item['fixture_id']} expected {item['expected_decision']}/{item['expected_reason_code']}, got {observed}/{reason}",
            )
        executed.append(str(item["fixture_id"]))
    return executed
