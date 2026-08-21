from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import subprocess
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from authority.common import canonical_hash, sha256_bytes
from authority.consumer_lock import (
    AUTHORITY_SYNC_TASK_ID,
    CONSUMER_LOCK_PATH,
    KERNEL_BUILD_EVIDENCE_POLICY_PATH,
    PACKAGE_SKELETON_TASK_ID,
    assert_phase00_consumer_lock_absent,
    authorize_consumer_lock_use,
    build_authority_consumer_lock,
    compute_kernel_source_subtree_hash,
    make_committed_consumer_lock_receipt,
    make_consumer_lock_readiness_receipt,
    validate_authority_consumer_lock,
    validate_authority_consumer_lock_structure,
    verify_consumer_lock_readiness_receipt,
    verify_kernel_build_evidence,
)
from authority.errors import GateViolation
from authority.receipts import make_typed_receipt

REPO_ROOT = Path(__file__).parents[2]


def _policy() -> dict[str, object]:
    return yaml.safe_load((REPO_ROOT / "governance/consumer-lock-policy.yaml").read_text())


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
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _consumer_repo(tmp_path: Path) -> tuple[Path, str]:
    root = tmp_path / "consumer"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "seed.txt").write_text("consumer seed\n", encoding="utf-8")
    return root, _commit(root, "consumer seed")


def _record_hash(raw: bytes) -> str:
    digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
    return f"sha256={digest}"


def _write_wheel(
    path: Path,
    *,
    version: str = "2.1.3.dev1",
    package_bytes: bytes = b"VALUE = 2\n",
    extra_files: dict[str, bytes] | None = None,
) -> None:
    dist_info = f"autocut_kernel-{version}.dist-info"
    files = {
        "autocut_kernel/__init__.py": package_bytes,
        f"{dist_info}/WHEEL": (
            b"Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{dist_info}/METADATA": (
            f"Metadata-Version: 2.1\nName: autocut-kernel\nVersion: {version}\n".encode()
        ),
    }
    files.update(extra_files or {})
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    for name, raw in files.items():
        writer.writerow((name, _record_hash(raw), str(len(raw))))
    record_path = f"{dist_info}/RECORD"
    writer.writerow((record_path, "", ""))
    files[record_path] = output.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, raw in files.items():
            archive.writestr(name, raw)


def _kernel_evidence_fixture(
    tmp_path: Path, *, builder_id: str = "autocut-isolated-wheel-builder-v1"
) -> tuple[object, dict[str, Path | str]]:
    root = tmp_path / "authority"
    root.mkdir()
    _git(root, "init", "-b", "main")
    policy_path = root / KERNEL_BUILD_EVIDENCE_POLICY_PATH
    policy_path.parent.mkdir(parents=True)
    policy_raw = (REPO_ROOT / KERNEL_BUILD_EVIDENCE_POLICY_PATH).read_bytes()
    policy_path.write_bytes(policy_raw)
    kernel_file = root / "packages/autocut-kernel/src/autocut_kernel/__init__.py"
    kernel_file.parent.mkdir(parents=True)
    kernel_file.write_text("VALUE = 1\n", encoding="utf-8")
    source_marker = root / "authority-source.txt"
    source_marker.write_text("authority source\n", encoding="utf-8")
    seed = _commit(root, "authority sources")
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
            "sha256": sha256_bytes(source_marker.read_bytes()),
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
    authority_commit = _commit(root, "seal authority")
    kernel_file.write_text("VALUE = 2\n", encoding="utf-8")
    kernel_commit = _commit(root, "kernel source")

    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    wheel_path = evidence_root / "autocut_kernel-2.1.3.dev1-py3-none-any.whl"
    _write_wheel(wheel_path)
    build_recipe_path = evidence_root / "build-recipe.json"
    build_recipe_path.write_text('{"command":["python","-m","build"]}\n', encoding="utf-8")
    environment_lock_path = evidence_root / "environment.lock"
    environment_lock_path.write_text("python==3.11.13\nbuild==1.3.0\n", encoding="utf-8")
    subtree_hash = compute_kernel_source_subtree_hash(root, kernel_commit)
    provenance = {
        "schema_version": "1.0.0",
        "receipt_type": "kernel_build_provenance",
        "builder_id": builder_id,
        "decision": "allow",
        "build_evidence_policy_hash": sha256_bytes(policy_raw),
        "kernel_source_commit": kernel_commit,
        "kernel_source_subtree_hash": subtree_hash,
        "wheel_sha256": sha256_bytes(wheel_path.read_bytes()),
        "build_recipe_hash": sha256_bytes(build_recipe_path.read_bytes()),
        "environment_lock_hash": sha256_bytes(environment_lock_path.read_bytes()),
    }
    provenance_path = evidence_root / "provenance.json"
    provenance_path.write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
    paths: dict[str, Path | str] = {
        "root": root,
        "authority_commit": authority_commit,
        "kernel_commit": kernel_commit,
        "wheel": wheel_path,
        "recipe": build_recipe_path,
        "environment": environment_lock_path,
        "provenance": provenance_path,
    }
    evidence = verify_kernel_build_evidence(
        authority_repository_root=root,
        authority_governance_commit=authority_commit,
        kernel_repository_root=root,
        kernel_source_commit=kernel_commit,
        wheel_path=wheel_path,
        distribution_version="2.1.3.dev1",
        build_recipe_path=build_recipe_path,
        environment_lock_path=environment_lock_path,
        provenance_receipt_path=provenance_path,
    )
    return evidence, paths


def _bootstrap_lock(evidence: object) -> dict[str, object]:
    return build_authority_consumer_lock(
        kernel_build_evidence=cast(Any, evidence),
        eligibility_profile="bootstrap_consumable",
        profile_policy=_policy(),
    )


def test_phase00_readiness_binds_commit_and_index_trees(tmp_path: Path) -> None:
    root, commit = _consumer_repo(tmp_path)
    receipt = make_consumer_lock_readiness_receipt(
        task_id=AUTHORITY_SYNC_TASK_ID,
        authority_governance_commit="a" * 40,
        authority_bundle_hash="sha256:" + "1" * 64,
        consumer_repository_root=root,
        consumer_repository_commit=commit,
        profile_policy=_policy(),
    )
    assert receipt["decision"] == "not_applicable"
    assert receipt["consumer_lock_path"] == CONSUMER_LOCK_PATH
    assert receipt["consumer_commit_tree_oid"] == _git(root, "rev-parse", f"{commit}^{{tree}}")
    assert receipt["consumer_index_tree_oid"] == _git(root, "write-tree")


def test_readiness_verifier_replays_mutable_index_after_issuance(tmp_path: Path) -> None:
    root, commit = _consumer_repo(tmp_path)
    authority_commit = "a" * 40
    authority_hash = "sha256:" + "1" * 64
    receipt = make_consumer_lock_readiness_receipt(
        task_id=AUTHORITY_SYNC_TASK_ID,
        authority_governance_commit=authority_commit,
        authority_bundle_hash=authority_hash,
        consumer_repository_root=root,
        consumer_repository_commit=commit,
        profile_policy=_policy(),
    )
    target = root / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.write_text("staged: true\n", encoding="utf-8")
    _git(root, "add", CONSUMER_LOCK_PATH)
    target.unlink()
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-LOCK-INDEXED"):
        verify_consumer_lock_readiness_receipt(
            receipt=receipt,
            consumer_repository_root=root,
            profile_policy=_policy(),
            expected_authority_governance_commit=authority_commit,
            expected_authority_bundle_hash=authority_hash,
        )


def test_readiness_rejects_caller_path_override() -> None:
    fields = {
        "task_id": AUTHORITY_SYNC_TASK_ID,
        "authority_governance_commit": "a" * 40,
        "authority_bundle_hash": "sha256:" + "1" * 64,
        "consumer_repository_commit": "b" * 40,
        "consumer_commit_tree_oid": "c" * 40,
        "consumer_index_tree_oid": "d" * 40,
        "consumer_lock_path": "governance/another.lock.yaml",
        "state": "not_materialized",
        "reason": "kernel_build_not_yet_available",
        "profile_policy_hash": canonical_hash(_policy()),
    }
    with pytest.raises(GateViolation, match="AUTH-RECEIPT-FIELD"):
        make_typed_receipt(
            "consumer_lock_readiness",
            authority_lock_hash=fields["authority_bundle_hash"],
            decision="not_applicable",
            reason_codes=[],
            **fields,
        )


def test_readiness_rejects_committed_lock_even_when_worktree_deleted(tmp_path: Path) -> None:
    root, _seed = _consumer_repo(tmp_path)
    target = root / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.write_text("committed: true\n", encoding="utf-8")
    commit = _commit(root, "commit forbidden lock")
    target.unlink()
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-LOCK-COMMITTED"):
        assert_phase00_consumer_lock_absent(
            consumer_repository_root=root, consumer_repository_commit=commit
        )


def test_readiness_rejects_indexed_skip_worktree_lock(tmp_path: Path) -> None:
    root, commit = _consumer_repo(tmp_path)
    target = root / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.write_text("indexed: true\n", encoding="utf-8")
    _git(root, "add", CONSUMER_LOCK_PATH)
    _git(root, "update-index", "--skip-worktree", CONSUMER_LOCK_PATH)
    target.unlink()
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-LOCK-INDEXED"):
        assert_phase00_consumer_lock_absent(
            consumer_repository_root=root, consumer_repository_commit=commit
        )


def test_readiness_rejects_worktree_lock(tmp_path: Path) -> None:
    root, commit = _consumer_repo(tmp_path)
    target = root / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.write_text("worktree: true\n", encoding="utf-8")
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-LOCK-WORKTREE"):
        assert_phase00_consumer_lock_absent(
            consumer_repository_root=root, consumer_repository_commit=commit
        )


def test_readiness_rejects_broken_worktree_symlink(tmp_path: Path) -> None:
    root, commit = _consumer_repo(tmp_path)
    target = root / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.symlink_to("missing-lock-target")
    with pytest.raises(GateViolation, match="AUTH-PATH-SYMLINK"):
        assert_phase00_consumer_lock_absent(
            consumer_repository_root=root, consumer_repository_commit=commit
        )


def test_only_canonical_child00_can_issue_readiness(tmp_path: Path) -> None:
    root, commit = _consumer_repo(tmp_path)
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-READINESS-TASK"):
        make_consumer_lock_readiness_receipt(
            task_id="00-trellis-authority-sync",
            authority_governance_commit="a" * 40,
            authority_bundle_hash="sha256:" + "1" * 64,
            consumer_repository_root=root,
            consumer_repository_commit=commit,
            profile_policy=_policy(),
        )


def test_fabricated_kernel_receipt_cannot_materialize_lock() -> None:
    with pytest.raises(GateViolation, match="AUTH-KERNEL-EVIDENCE-UNVERIFIED"):
        build_authority_consumer_lock(
            kernel_build_evidence=cast(Any, {"receipt_type": "kernel_build"}),
            eligibility_profile="bootstrap_consumable",
            profile_policy=_policy(),
        )


@pytest.mark.parametrize("changed", ["wheel", "recipe", "environment", "provenance"])
def test_materialization_replays_and_rejects_altered_build_evidence(
    tmp_path: Path, changed: str
) -> None:
    evidence, paths = _kernel_evidence_fixture(tmp_path)
    path = cast(Path, paths[changed])
    path.write_bytes(path.read_bytes() + b"altered\n")
    with pytest.raises(GateViolation, match="AUTH-(?:KERNEL|DOCUMENT-INVALID)"):
        _bootstrap_lock(evidence)


def test_unapproved_provenance_builder_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GateViolation, match="AUTH-KERNEL-BUILDER"):
        _kernel_evidence_fixture(tmp_path, builder_id="caller-self-report")


def test_consumer_lock_rejects_placeholder_hash_mismatch_and_self_reference(
    tmp_path: Path,
) -> None:
    evidence, _paths = _kernel_evidence_fixture(tmp_path)
    lock = _bootstrap_lock(evidence)
    placeholder = dict(lock)
    placeholder["distribution_version"] = "pending"
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-LOCK-PLACEHOLDER"):
        validate_authority_consumer_lock_structure(placeholder, profile_policy=_policy())
    mismatch = dict(lock)
    mismatch["wheel_sha256"] = "sha256:" + "8" * 64
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-KERNEL-MISMATCH"):
        validate_authority_consumer_lock(
            mismatch,
            profile_policy=_policy(),
            kernel_build_evidence=cast(Any, evidence),
        )
    self_reference = dict(lock)
    self_reference["consumer_repository_commit"] = "c" * 40
    with pytest.raises(GateViolation, match="AUTH-SCHEMA-EXTRA"):
        validate_authority_consumer_lock_structure(self_reference, profile_policy=_policy())


def test_wheel_with_valid_metadata_but_different_code_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GateViolation, match="AUTH-KERNEL-WHEEL-SOURCE-MISMATCH"):
        _evidence, paths = _kernel_evidence_fixture(tmp_path)
        _write_wheel(cast(Path, paths["wheel"]), package_bytes=b"VALUE = 999\n")
        # Recreate provenance to prove that caller-consistent metadata and hashes are insufficient.
        provenance_path = cast(Path, paths["provenance"])
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["wheel_sha256"] = sha256_bytes(cast(Path, paths["wheel"]).read_bytes())
        provenance_path.write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
        verify_kernel_build_evidence(
            authority_repository_root=cast(Path, paths["root"]),
            authority_governance_commit=cast(str, paths["authority_commit"]),
            kernel_repository_root=cast(Path, paths["root"]),
            kernel_source_commit=cast(str, paths["kernel_commit"]),
            wheel_path=cast(Path, paths["wheel"]),
            distribution_version="2.1.3.dev1",
            build_recipe_path=cast(Path, paths["recipe"]),
            environment_lock_path=cast(Path, paths["environment"]),
            provenance_receipt_path=provenance_path,
        )


def test_wheel_with_extra_executable_code_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(GateViolation, match="AUTH-KERNEL-WHEEL-EXTRA"):
        _evidence, paths = _kernel_evidence_fixture(tmp_path)
        _write_wheel(cast(Path, paths["wheel"]), extra_files={"injected.py": b"RUN = True\n"})
        provenance_path = cast(Path, paths["provenance"])
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance["wheel_sha256"] = sha256_bytes(cast(Path, paths["wheel"]).read_bytes())
        provenance_path.write_text(json.dumps(provenance, sort_keys=True) + "\n", encoding="utf-8")
        verify_kernel_build_evidence(
            authority_repository_root=cast(Path, paths["root"]),
            authority_governance_commit=cast(str, paths["authority_commit"]),
            kernel_repository_root=cast(Path, paths["root"]),
            kernel_source_commit=cast(str, paths["kernel_commit"]),
            wheel_path=cast(Path, paths["wheel"]),
            distribution_version="2.1.3.dev1",
            build_recipe_path=cast(Path, paths["recipe"]),
            environment_lock_path=cast(Path, paths["environment"]),
            provenance_receipt_path=provenance_path,
        )


@pytest.mark.parametrize(
    "capability",
    [
        "command_dispatch",
        "authority_store_write",
        "business_execution",
        "shadow_output",
        "publication",
    ],
)
def test_bootstrap_lock_cannot_gain_runtime_or_publish_capability(
    tmp_path: Path, capability: str
) -> None:
    evidence, _paths = _kernel_evidence_fixture(tmp_path)
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-CAPABILITY-DENY"):
        authorize_consumer_lock_use(
            lock=_bootstrap_lock(evidence),
            profile_policy=_policy(),
            requested_capabilities=[capability],
            verified_post_commit_receipts=[],
            kernel_build_evidence=cast(Any, evidence),
        )


def test_post_commit_and_runtime_replay_git_and_kernel_evidence(tmp_path: Path) -> None:
    evidence, _paths = _kernel_evidence_fixture(tmp_path)
    consumer_root = tmp_path / "consumer"
    consumer_root.mkdir()
    _git(consumer_root, "init", "-b", "main")
    lock = _bootstrap_lock(evidence)
    target = consumer_root / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
    commit = _commit(consumer_root, "materialize consumer lock")
    receipt = make_committed_consumer_lock_receipt(
        task_id=PACKAGE_SKELETON_TASK_ID,
        consumer_repository_root=consumer_root,
        consumer_repository_commit=commit,
        profile_policy=_policy(),
        kernel_build_evidence=cast(Any, evidence),
    )
    authorize_consumer_lock_use(
        lock=lock,
        profile_policy=_policy(),
        requested_capabilities=["kernel_import", "packaging_smoke"],
        verified_post_commit_receipts=[receipt],
        kernel_build_evidence=cast(Any, evidence),
        consumer_repository_root=consumer_root,
    )
