# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Fail-closed authority consumer-lock contracts.

Phase -1 owns these pure contracts but never writes the consumer lock.  The
first lock can only be materialized after Phase 02 has produced a verified
kernel wheel and ``KernelBuildReceipt``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

from .common import (
    canonical_hash,
    contained_path,
    git_bytes,
    git_output,
    load_mapping_bytes,
    require_closed,
    require_commit,
    require_list,
    require_non_empty_string,
    require_sha256,
    sha256_bytes,
    validate_relative_path,
)
from .errors import GateViolation
from .receipts import TITLES, make_typed_receipt, validate_typed_receipt

CONSUMER_LOCK_PATH = "governance/authority-consumer.lock.yaml"
READINESS_REASON = "kernel_build_not_yet_available"
PROFILE_NAMES = (
    "bootstrap_consumable",
    "execution_eligible",
    "shadow_eligible",
    "publication_eligible",
)
CAPABILITY_NAMES = (
    "kernel_import",
    "packaging_smoke",
    "command_dispatch",
    "authority_store_write",
    "business_execution",
    "shadow_output",
    "publication",
)
_FORBIDDEN_LOCK_TOKEN = re.compile(r"(?i)(?:pending|placeholder|not[_ -]?materialized|tbd|todo)")


def _require_boolean(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise GateViolation("AUTH-CONSUMER-POLICY", f"{where} must be boolean")
    return value


def _require_positive_integer(value: Any, *, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GateViolation("AUTH-CONSUMER-LOCK-FIELD", f"{where} must be a positive integer")
    return value


def _require_unique_strings(value: Any, *, where: str, non_empty: bool = True) -> list[str]:
    items = require_list(value, where=where, non_empty=non_empty)
    if not all(isinstance(item, str) and item for item in items):
        raise GateViolation("AUTH-CONSUMER-POLICY", f"{where} must contain strings")
    strings = cast(list[str], items)
    if len(strings) != len(set(strings)):
        raise GateViolation("AUTH-CONSUMER-POLICY", f"{where} contains duplicates")
    if strings != sorted(strings):
        raise GateViolation("AUTH-CONSUMER-POLICY", f"{where} must use canonical sort order")
    return strings


def validate_consumer_lock_policy(policy: Mapping[str, Any]) -> None:
    """Validate all profiles, receipt prerequisites and capability ceilings."""

    require_closed(
        policy,
        required=("schema_version", "profiles"),
        where="consumer lock policy",
    )
    if policy["schema_version"] != "1.0.0":
        raise GateViolation("AUTH-CONSUMER-POLICY", "unsupported policy version")
    profiles = policy["profiles"]
    if not isinstance(profiles, dict) or set(profiles) != set(PROFILE_NAMES):
        raise GateViolation(
            "AUTH-CONSUMER-POLICY",
            "profiles must contain exactly the four canonical profiles",
        )
    previous_capabilities: dict[str, bool] | None = None
    previous_materialization: set[str] = set()
    for profile_name in PROFILE_NAMES:
        profile = profiles[profile_name]
        if not isinstance(profile, dict):
            raise GateViolation("AUTH-CONSUMER-POLICY", f"{profile_name} must be an object")
        require_closed(
            profile,
            required=(
                "materialization_required_receipt_types",
                "post_commit_required_receipt_types",
                "capability_ceiling",
            ),
            where=f"consumer lock profile {profile_name}",
        )
        materialization = set(
            _require_unique_strings(
                profile["materialization_required_receipt_types"],
                where=f"{profile_name}.materialization_required_receipt_types",
            )
        )
        post_commit = _require_unique_strings(
            profile["post_commit_required_receipt_types"],
            where=f"{profile_name}.post_commit_required_receipt_types",
        )
        if "KernelBuildReceipt" not in materialization:
            raise GateViolation(
                "AUTH-CONSUMER-POLICY", f"{profile_name} must require KernelBuildReceipt"
            )
        if post_commit != ["ConsumerLockReceipt"]:
            raise GateViolation(
                "AUTH-CONSUMER-POLICY",
                f"{profile_name} must require exactly the post-commit ConsumerLockReceipt",
            )
        if not previous_materialization.issubset(materialization):
            raise GateViolation(
                "AUTH-CONSUMER-POLICY", "profile promotion cannot remove receipt requirements"
            )
        previous_materialization = materialization
        ceiling = profile["capability_ceiling"]
        if not isinstance(ceiling, dict):
            raise GateViolation("AUTH-CONSUMER-POLICY", "capability ceiling must be an object")
        require_closed(
            ceiling,
            required=CAPABILITY_NAMES,
            where=f"{profile_name}.capability_ceiling",
        )
        capabilities = {
            name: _require_boolean(ceiling[name], where=f"{profile_name}.{name}")
            for name in CAPABILITY_NAMES
        }
        if not capabilities["kernel_import"] or not capabilities["packaging_smoke"]:
            raise GateViolation(
                "AUTH-CONSUMER-POLICY", "every consumable profile needs import/smoke"
            )
        implications = (
            ("authority_store_write", "command_dispatch"),
            ("business_execution", "authority_store_write"),
            ("shadow_output", "business_execution"),
            ("publication", "shadow_output"),
        )
        if any(capabilities[left] and not capabilities[right] for left, right in implications):
            raise GateViolation(
                "AUTH-CONSUMER-POLICY", "capability ceiling violates promotion dependencies"
            )
        if previous_capabilities is not None and any(
            previous_capabilities[name] and not capabilities[name] for name in CAPABILITY_NAMES
        ):
            raise GateViolation(
                "AUTH-CONSUMER-POLICY", "profile promotion cannot remove a capability"
            )
        previous_capabilities = capabilities

    bootstrap = profiles["bootstrap_consumable"]["capability_ceiling"]
    expected_bootstrap = {
        "kernel_import": True,
        "packaging_smoke": True,
        "command_dispatch": False,
        "authority_store_write": False,
        "business_execution": False,
        "shadow_output": False,
        "publication": False,
    }
    if bootstrap != expected_bootstrap:
        raise GateViolation(
            "AUTH-CONSUMER-POLICY",
            "bootstrap_consumable is limited to kernel import and packaging smoke",
        )


def _reject_lock_placeholders(value: Any, *, where: str = "consumer lock") -> None:
    if isinstance(value, str) and _FORBIDDEN_LOCK_TOKEN.search(value):
        raise GateViolation("AUTH-CONSUMER-LOCK-PLACEHOLDER", f"{where} contains a state token")
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_lock_placeholders(item, where=f"{where}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_lock_placeholders(item, where=f"{where}[{index}]")


def validate_authority_consumer_lock(
    lock: Mapping[str, Any],
    *,
    profile_policy: Mapping[str, Any],
    kernel_build_receipt: Mapping[str, Any] | None = None,
) -> None:
    """Validate a materialized lock and independently replay its frozen evidence."""

    validate_consumer_lock_policy(profile_policy)
    require_closed(
        lock,
        required=(
            "schema_version",
            "contract_version",
            "authority_governance_commit",
            "authority_lock_document_hash",
            "authority_bundle_hash",
            "kernel_source_commit",
            "kernel_source_subtree_hash",
            "distribution_name",
            "distribution_version",
            "wheel_filename",
            "wheel_tag",
            "wheel_size_bytes",
            "wheel_sha256",
            "build_recipe_hash",
            "environment_lock_hash",
            "provenance_receipt_hash",
            "eligibility_profile",
            "profile_policy_hash",
            "materialization_receipts",
        ),
        where="AuthorityConsumerLock",
    )
    _reject_lock_placeholders(lock)
    if lock["schema_version"] != "1.0.0" or lock["contract_version"] != "2.1.3":
        raise GateViolation("AUTH-CONSUMER-LOCK-VERSION", "unsupported consumer lock version")
    require_commit(lock["authority_governance_commit"], where="authority_governance_commit")
    for field in (
        "authority_lock_document_hash",
        "authority_bundle_hash",
        "kernel_source_subtree_hash",
        "wheel_sha256",
        "build_recipe_hash",
        "environment_lock_hash",
        "provenance_receipt_hash",
        "profile_policy_hash",
    ):
        require_sha256(lock[field], where=field)
    require_commit(lock["kernel_source_commit"], where="kernel_source_commit")
    for field in ("distribution_name", "distribution_version", "wheel_tag"):
        require_non_empty_string(lock[field], where=field)
    wheel_filename = require_non_empty_string(lock["wheel_filename"], where="wheel_filename")
    if "/" in wheel_filename or "\\" in wheel_filename or not wheel_filename.endswith(".whl"):
        raise GateViolation(
            "AUTH-CONSUMER-LOCK-FIELD", "wheel_filename must be a basename ending in .whl"
        )
    _require_positive_integer(lock["wheel_size_bytes"], where="wheel_size_bytes")
    profile_name = require_non_empty_string(lock["eligibility_profile"], where="profile")
    if profile_name not in PROFILE_NAMES:
        raise GateViolation("AUTH-CONSUMER-LOCK-PROFILE", f"unknown profile: {profile_name}")
    if lock["profile_policy_hash"] != canonical_hash(profile_policy):
        raise GateViolation("AUTH-CONSUMER-POLICY-HASH", "profile policy hash mismatch")

    profile = profile_policy["profiles"][profile_name]
    required_types = profile["materialization_required_receipt_types"]
    evidence = require_list(
        lock["materialization_receipts"], where="materialization_receipts", non_empty=True
    )
    observed: dict[str, str] = {}
    for index, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-CONSUMER-EVIDENCE", f"evidence {index} must be an object")
        require_closed(
            item,
            required=("receipt_type", "receipt_hash"),
            where=f"materialization receipt {index}",
        )
        receipt_type = require_non_empty_string(item["receipt_type"], where="receipt_type")
        receipt_hash = require_sha256(item["receipt_hash"], where="receipt_hash")
        if receipt_type in observed:
            raise GateViolation("AUTH-CONSUMER-EVIDENCE", "duplicate receipt type")
        observed[receipt_type] = receipt_hash
    if list(observed) != sorted(observed) or list(observed) != required_types:
        raise GateViolation(
            "AUTH-CONSUMER-EVIDENCE", "materialization receipts do not match profile"
        )

    if kernel_build_receipt is None:
        return
    validate_typed_receipt(kernel_build_receipt, expected_type="kernel_build")
    if kernel_build_receipt["decision"] != "allow":
        raise GateViolation("AUTH-CONSUMER-KERNEL-DENY", "kernel build receipt must allow")
    if kernel_build_receipt["authority_lock_hash"] != lock["authority_bundle_hash"]:
        raise GateViolation("AUTH-CONSUMER-AUTHORITY-HASH", "receipt authority hash mismatch")
    comparisons = {
        "authority_governance_commit": "authority_governance_commit",
        "authority_lock_document_hash": "authority_lock_document_hash",
        "authority_bundle_hash": "authority_bundle_hash",
        "kernel_source_commit": "kernel_source_commit",
        "kernel_source_subtree_hash": "kernel_source_subtree_hash",
        "distribution_name": "distribution_name",
        "distribution_version": "distribution_version",
        "wheel_filename": "wheel_filename",
        "wheel_tag": "wheel_tag",
        "wheel_size_bytes": "wheel_size_bytes",
        "wheel_sha256": "wheel_sha256",
        "build_recipe_hash": "build_recipe_hash",
        "environment_lock_hash": "environment_lock_hash",
        "provenance_receipt_hash": "provenance_receipt_hash",
    }
    if any(lock[left] != kernel_build_receipt[right] for left, right in comparisons.items()):
        raise GateViolation("AUTH-CONSUMER-KERNEL-MISMATCH", "lock differs from kernel receipt")
    if observed.get("KernelBuildReceipt") != kernel_build_receipt["receipt_id"]:
        raise GateViolation("AUTH-CONSUMER-KERNEL-MISMATCH", "kernel receipt hash mismatch")


def build_authority_consumer_lock(
    *,
    kernel_build_receipt: Mapping[str, Any],
    eligibility_profile: str,
    profile_policy: Mapping[str, Any],
    verified_materialization_receipt_hashes: Mapping[str, str],
) -> dict[str, Any]:
    """Return deterministic lock bytes as data; this function never writes a file."""

    validate_typed_receipt(kernel_build_receipt, expected_type="kernel_build")
    if kernel_build_receipt["decision"] != "allow":
        raise GateViolation("AUTH-CONSUMER-KERNEL-DENY", "kernel build receipt must allow")
    validate_consumer_lock_policy(profile_policy)
    if eligibility_profile not in PROFILE_NAMES:
        raise GateViolation("AUTH-CONSUMER-LOCK-PROFILE", "unknown eligibility profile")
    hashes = dict(verified_materialization_receipt_hashes)
    hashes.setdefault("KernelBuildReceipt", str(kernel_build_receipt["receipt_id"]))
    materialization_receipts = [
        {"receipt_type": receipt_type, "receipt_hash": hashes[receipt_type]}
        for receipt_type in sorted(hashes)
    ]
    lock = {
        "schema_version": "1.0.0",
        "contract_version": "2.1.3",
        "authority_governance_commit": kernel_build_receipt["authority_governance_commit"],
        "authority_lock_document_hash": kernel_build_receipt["authority_lock_document_hash"],
        "authority_bundle_hash": kernel_build_receipt["authority_bundle_hash"],
        "kernel_source_commit": kernel_build_receipt["kernel_source_commit"],
        "kernel_source_subtree_hash": kernel_build_receipt["kernel_source_subtree_hash"],
        "distribution_name": kernel_build_receipt["distribution_name"],
        "distribution_version": kernel_build_receipt["distribution_version"],
        "wheel_filename": kernel_build_receipt["wheel_filename"],
        "wheel_tag": kernel_build_receipt["wheel_tag"],
        "wheel_size_bytes": kernel_build_receipt["wheel_size_bytes"],
        "wheel_sha256": kernel_build_receipt["wheel_sha256"],
        "build_recipe_hash": kernel_build_receipt["build_recipe_hash"],
        "environment_lock_hash": kernel_build_receipt["environment_lock_hash"],
        "provenance_receipt_hash": kernel_build_receipt["provenance_receipt_hash"],
        "eligibility_profile": eligibility_profile,
        "profile_policy_hash": canonical_hash(profile_policy),
        "materialization_receipts": materialization_receipts,
    }
    validate_authority_consumer_lock(
        lock, profile_policy=profile_policy, kernel_build_receipt=kernel_build_receipt
    )
    return lock


def assert_phase00_consumer_lock_absent(
    *, consumer_repository_root: Path, consumer_lock_path: str = CONSUMER_LOCK_PATH
) -> None:
    """Prove Phase 00 has not installed a placeholder or real consumer lock."""

    relative = validate_relative_path(consumer_lock_path, where="consumer lock path")
    target = contained_path(consumer_repository_root, relative)
    if target.exists():
        raise GateViolation(
            "AUTH-CONSUMER-LOCK-PREMATURE",
            "Phase 00 must not materialize authority-consumer.lock.yaml",
        )


def make_consumer_lock_readiness_receipt(
    *,
    task_id: str,
    authority_governance_commit: str,
    authority_bundle_hash: str,
    consumer_repository_root: Path,
    consumer_repository_commit: str,
    profile_policy: Mapping[str, Any],
    consumer_lock_path: str = CONSUMER_LOCK_PATH,
) -> dict[str, Any]:
    """Issue the only legal Phase-00 consumer-lock predicate."""

    if task_id != "00-trellis-authority-sync":
        raise GateViolation(
            "AUTH-CONSUMER-READINESS-TASK",
            "only child 00 may issue the not-materialized readiness receipt",
        )
    validate_consumer_lock_policy(profile_policy)
    assert_phase00_consumer_lock_absent(
        consumer_repository_root=consumer_repository_root,
        consumer_lock_path=consumer_lock_path,
    )
    return make_typed_receipt(
        "consumer_lock_readiness",
        authority_lock_hash=authority_bundle_hash,
        decision="not_applicable",
        reason_codes=[],
        task_id=task_id,
        authority_governance_commit=authority_governance_commit,
        authority_bundle_hash=authority_bundle_hash,
        consumer_repository_commit=consumer_repository_commit,
        consumer_lock_path=consumer_lock_path,
        state="not_materialized",
        reason=READINESS_REASON,
        profile_policy_hash=canonical_hash(profile_policy),
    )


def make_kernel_build_receipt(
    *,
    authority_bundle_hash: str,
    decision: str = "allow",
    reason_codes: Sequence[str] = (),
    **fields: Any,
) -> dict[str, Any]:
    """Create the Phase-02 kernel build evidence with one canonical contract."""

    return make_typed_receipt(
        "kernel_build",
        authority_lock_hash=authority_bundle_hash,
        decision=decision,
        reason_codes=reason_codes,
        authority_bundle_hash=authority_bundle_hash,
        **fields,
    )


def make_committed_consumer_lock_receipt(
    *,
    task_id: str,
    consumer_repository_root: Path,
    consumer_repository_commit: str,
    profile_policy: Mapping[str, Any],
    consumer_lock_path: str = CONSUMER_LOCK_PATH,
) -> dict[str, Any]:
    """Bind a committed lock blob and consumer tree without lock self-reference."""

    relative = validate_relative_path(consumer_lock_path, where="consumer lock path")
    raw = git_bytes(consumer_repository_root, consumer_repository_commit, relative)
    lock = load_mapping_bytes(raw, where=f"{consumer_repository_commit}:{relative}")
    validate_authority_consumer_lock(lock, profile_policy=profile_policy)
    tree_oid = git_output(
        consumer_repository_root, "rev-parse", f"{consumer_repository_commit}^{{tree}}"
    )
    require_commit(tree_oid, where="consumer commit tree")
    return make_typed_receipt(
        "consumer_lock",
        authority_lock_hash=str(lock["authority_bundle_hash"]),
        decision="allow",
        reason_codes=[],
        task_id=task_id,
        authority_governance_commit=lock["authority_governance_commit"],
        authority_bundle_hash=lock["authority_bundle_hash"],
        kernel_build_receipt_hash=dict(
            (item["receipt_type"], item["receipt_hash"])
            for item in cast(list[dict[str, str]], lock["materialization_receipts"])
        )["KernelBuildReceipt"],
        consumer_lock_blob_hash=sha256_bytes(raw),
        consumer_lock_document_hash=canonical_hash(lock),
        consumer_repository_commit=consumer_repository_commit,
        consumer_commit_tree_oid=tree_oid,
        eligibility_profile=lock["eligibility_profile"],
        profile_policy_hash=lock["profile_policy_hash"],
    )


def verify_committed_consumer_lock_receipt(
    *,
    receipt: Mapping[str, Any],
    consumer_repository_root: Path,
    lock: Mapping[str, Any],
    profile_policy: Mapping[str, Any],
    consumer_lock_path: str = CONSUMER_LOCK_PATH,
) -> None:
    """Replay a post-commit receipt from immutable Git objects."""

    validate_typed_receipt(receipt, expected_type="consumer_lock")
    if receipt["decision"] != "allow":
        raise GateViolation("AUTH-CONSUMER-RECEIPT-DENY", "consumer lock receipt did not allow")
    relative = validate_relative_path(consumer_lock_path, where="consumer lock path")
    commit = str(receipt["consumer_repository_commit"])
    raw = git_bytes(consumer_repository_root, commit, relative)
    committed_lock = load_mapping_bytes(raw, where=f"{commit}:{relative}")
    validate_authority_consumer_lock(committed_lock, profile_policy=profile_policy)
    tree_oid = git_output(consumer_repository_root, "rev-parse", f"{commit}^{{tree}}")
    materialization_receipts = {
        str(item["receipt_type"]): str(item["receipt_hash"])
        for item in cast(list[dict[str, str]], committed_lock["materialization_receipts"])
    }
    comparisons = {
        "authority_lock_hash": committed_lock["authority_bundle_hash"],
        "consumer_lock_blob_hash": sha256_bytes(raw),
        "consumer_lock_document_hash": canonical_hash(committed_lock),
        "consumer_commit_tree_oid": tree_oid,
        "authority_governance_commit": committed_lock["authority_governance_commit"],
        "authority_bundle_hash": committed_lock["authority_bundle_hash"],
        "eligibility_profile": committed_lock["eligibility_profile"],
        "profile_policy_hash": committed_lock["profile_policy_hash"],
        "kernel_build_receipt_hash": materialization_receipts["KernelBuildReceipt"],
    }
    if any(receipt[field] != expected for field, expected in comparisons.items()):
        raise GateViolation(
            "AUTH-CONSUMER-LOCK-RECEIPT-MISMATCH",
            "post-commit receipt does not match committed lock/tree",
        )
    if canonical_hash(committed_lock) != canonical_hash(lock):
        raise GateViolation(
            "AUTH-CONSUMER-LOCK-RECEIPT-MISMATCH",
            "requested lock differs from committed lock",
        )


def authorize_consumer_lock_use(
    *,
    lock: Mapping[str, Any],
    profile_policy: Mapping[str, Any],
    requested_capabilities: Sequence[str],
    verified_post_commit_receipts: Sequence[Mapping[str, Any]],
    consumer_repository_root: Path | None = None,
) -> None:
    """Enforce receipt closure and the selected profile's capability ceiling."""

    validate_authority_consumer_lock(lock, profile_policy=profile_policy)
    profile_name = str(lock["eligibility_profile"])
    profile = profile_policy["profiles"][profile_name]
    ceiling = profile["capability_ceiling"]
    for capability in requested_capabilities:
        if capability not in CAPABILITY_NAMES:
            raise GateViolation(
                "AUTH-CONSUMER-CAPABILITY-UNKNOWN", f"unknown capability: {capability}"
            )
        if not ceiling[capability]:
            raise GateViolation(
                "AUTH-CONSUMER-CAPABILITY-DENY",
                f"{profile_name} does not authorize {capability}",
            )

    observed_types: set[str] = set()
    for receipt in verified_post_commit_receipts:
        validate_typed_receipt(receipt)
        if receipt["decision"] != "allow":
            raise GateViolation("AUTH-CONSUMER-RECEIPT-DENY", "receipt did not allow")
        receipt_type = str(receipt["receipt_type"])
        title = TITLES[receipt_type]
        if title in observed_types:
            raise GateViolation("AUTH-CONSUMER-RECEIPT-DUPLICATE", "duplicate receipt type")
        observed_types.add(title)
        if receipt_type == "consumer_lock":
            if consumer_repository_root is None:
                raise GateViolation(
                    "AUTH-CONSUMER-RECEIPT-UNVERIFIED",
                    "consumer repository root is required to replay the receipt",
                )
            verify_committed_consumer_lock_receipt(
                receipt=receipt,
                consumer_repository_root=consumer_repository_root,
                lock=lock,
                profile_policy=profile_policy,
            )
    required = set(profile["post_commit_required_receipt_types"])
    if not required.issubset(observed_types):
        raise GateViolation(
            "AUTH-CONSUMER-RECEIPT-MISSING",
            f"missing post-commit receipts: {sorted(required - observed_types)}",
        )
