from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml
from authority.common import canonical_hash
from authority.consumer_lock import (
    CONSUMER_LOCK_PATH,
    assert_phase00_consumer_lock_absent,
    authorize_consumer_lock_use,
    build_authority_consumer_lock,
    make_committed_consumer_lock_receipt,
    make_consumer_lock_readiness_receipt,
    make_kernel_build_receipt,
    validate_authority_consumer_lock,
)
from authority.errors import GateViolation
from authority.receipts import make_typed_receipt

REPO_ROOT = Path(__file__).parents[2]
HASH = "sha256:" + "1" * 64
DOCUMENT_HASH = "sha256:" + "2" * 64


def _policy() -> dict[str, object]:
    return yaml.safe_load((REPO_ROOT / "governance/consumer-lock-policy.yaml").read_text())


def _kernel_receipt() -> dict[str, object]:
    return make_kernel_build_receipt(
        authority_bundle_hash=HASH,
        task_id="02-import-firewall-and-package-skeleton",
        authority_governance_commit="a" * 40,
        authority_lock_document_hash=DOCUMENT_HASH,
        kernel_source_commit="b" * 40,
        kernel_source_subtree_hash="sha256:" + "3" * 64,
        distribution_name="autocut-kernel",
        distribution_version="2.1.3.dev1",
        wheel_filename="autocut_kernel-2.1.3.dev1-py3-none-any.whl",
        wheel_tag="py3-none-any",
        wheel_size_bytes=4096,
        wheel_sha256="sha256:" + "4" * 64,
        build_recipe_hash="sha256:" + "5" * 64,
        environment_lock_hash="sha256:" + "6" * 64,
        provenance_receipt_hash="sha256:" + "7" * 64,
    )


def _bootstrap_lock() -> dict[str, object]:
    receipt = _kernel_receipt()
    return build_authority_consumer_lock(
        kernel_build_receipt=receipt,
        eligibility_profile="bootstrap_consumable",
        profile_policy=_policy(),
        verified_materialization_receipt_hashes={"KernelBuildReceipt": str(receipt["receipt_id"])},
    )


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def test_phase00_issues_hashed_readiness_without_materializing_lock(tmp_path: Path) -> None:
    receipt = make_consumer_lock_readiness_receipt(
        task_id="00-trellis-authority-sync",
        authority_governance_commit="a" * 40,
        authority_bundle_hash=HASH,
        consumer_repository_root=tmp_path,
        consumer_repository_commit="b" * 40,
        profile_policy=_policy(),
    )
    assert receipt["decision"] == "not_applicable"
    assert receipt["state"] == "not_materialized"
    assert receipt["reason"] == "kernel_build_not_yet_available"
    assert receipt["profile_policy_hash"] == canonical_hash(_policy())
    assert not (tmp_path / CONSUMER_LOCK_PATH).exists()


def test_readiness_receipt_cannot_claim_allow_or_another_reason(tmp_path: Path) -> None:
    fields = {
        "task_id": "00-trellis-authority-sync",
        "authority_governance_commit": "a" * 40,
        "authority_bundle_hash": HASH,
        "consumer_repository_commit": "b" * 40,
        "consumer_lock_path": CONSUMER_LOCK_PATH,
        "state": "not_materialized",
        "reason": "kernel_build_not_yet_available",
        "profile_policy_hash": canonical_hash(_policy()),
    }
    with pytest.raises(GateViolation, match="AUTH-RECEIPT-DECISION"):
        make_typed_receipt(
            "consumer_lock_readiness",
            authority_lock_hash=HASH,
            decision="allow",
            reason_codes=[],
            **fields,
        )


def test_phase00_rejects_any_consumer_lock_instance(tmp_path: Path) -> None:
    target = tmp_path / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.write_text("state: pending\n", encoding="utf-8")
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-LOCK-PREMATURE"):
        assert_phase00_consumer_lock_absent(consumer_repository_root=tmp_path)


def test_only_child00_can_issue_not_materialized_readiness(tmp_path: Path) -> None:
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-READINESS-TASK"):
        make_consumer_lock_readiness_receipt(
            task_id="02-import-firewall-and-package-skeleton",
            authority_governance_commit="a" * 40,
            authority_bundle_hash=HASH,
            consumer_repository_root=tmp_path,
            consumer_repository_commit="b" * 40,
            profile_policy=_policy(),
        )


def test_package_skeleton_profile_activates_materialization_predicate() -> None:
    profiles = yaml.safe_load((REPO_ROOT / "governance/activation-profiles.yaml").read_text())[
        "profiles"
    ]
    assert profiles["authority_bootstrap"]["predicates"]["consumer_lock_materialized"] == {
        "minimum_phase": "phase_0"
    }
    assert profiles["authority_package_skeleton"]["predicates"]["consumer_lock_materialized"] == {
        "minimum_phase": "phase_minus_1"
    }


def test_consumer_lock_rejects_placeholder_hash_mismatch_and_self_reference() -> None:
    lock = _bootstrap_lock()
    receipt = _kernel_receipt()
    placeholder = dict(lock)
    placeholder["distribution_version"] = "pending"
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-LOCK-PLACEHOLDER"):
        validate_authority_consumer_lock(placeholder, profile_policy=_policy())

    mismatch = dict(lock)
    mismatch["wheel_sha256"] = "sha256:" + "8" * 64
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-KERNEL-MISMATCH"):
        validate_authority_consumer_lock(
            mismatch, profile_policy=_policy(), kernel_build_receipt=receipt
        )

    self_reference = dict(lock)
    self_reference["consumer_repository_commit"] = "c" * 40
    with pytest.raises(GateViolation, match="AUTH-SCHEMA-EXTRA"):
        validate_authority_consumer_lock(self_reference, profile_policy=_policy())


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
def test_bootstrap_consumer_lock_cannot_gain_runtime_or_publish_capability(
    capability: str,
) -> None:
    with pytest.raises(GateViolation, match="AUTH-CONSUMER-CAPABILITY-DENY"):
        authorize_consumer_lock_use(
            lock=_bootstrap_lock(),
            profile_policy=_policy(),
            requested_capabilities=[capability],
            verified_post_commit_receipts=[],
        )


def test_post_commit_receipt_binds_lock_blob_and_tree_without_self_reference(
    tmp_path: Path,
) -> None:
    _git(tmp_path, "init", "-b", "main")
    lock = _bootstrap_lock()
    target = tmp_path / CONSUMER_LOCK_PATH
    target.parent.mkdir(parents=True)
    target.write_text(yaml.safe_dump(lock, sort_keys=False), encoding="utf-8")
    _git(tmp_path, "add", CONSUMER_LOCK_PATH)
    _git(
        tmp_path,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-m",
        "materialize consumer lock",
    )
    commit = _git(tmp_path, "rev-parse", "HEAD")
    receipt = make_committed_consumer_lock_receipt(
        task_id="02-import-firewall-and-package-skeleton",
        consumer_repository_root=tmp_path,
        consumer_repository_commit=commit,
        profile_policy=_policy(),
    )
    assert receipt["consumer_repository_commit"] == commit
    assert "consumer_repository_commit" not in lock
    authorize_consumer_lock_use(
        lock=lock,
        profile_policy=_policy(),
        requested_capabilities=["kernel_import", "packaging_smoke"],
        verified_post_commit_receipts=[receipt],
        consumer_repository_root=tmp_path,
    )
