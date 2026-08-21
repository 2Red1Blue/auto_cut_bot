# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Fail-closed authority consumer-lock contracts.

Phase -1 owns these pure contracts but never writes the consumer lock.  The
first lock can only be materialized after Phase 02 has produced a verified
kernel wheel and ``KernelBuildReceipt``.
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from .common import (
    canonical_hash,
    contained_path,
    git_bytes,
    git_output,
    load_mapping_bytes,
    require_closed,
    require_commit,
    require_git_object_oid,
    require_list,
    require_non_empty_string,
    require_sha256,
    sha256_bytes,
    validate_relative_path,
)
from .errors import GateViolation
from .lock import validate_authority_lock
from .receipts import TITLES, make_typed_receipt, validate_typed_receipt

CONSUMER_LOCK_PATH = "governance/authority-consumer.lock.yaml"
AUTHORITY_LOCK_PATH = "governance/authority-lock.yaml"
KERNEL_SOURCE_SUBTREE_PATH = "packages/autocut-kernel"
KERNEL_PACKAGE_SOURCE_PATH = "packages/autocut-kernel/src/autocut_kernel"
WHEEL_PACKAGE_PATH = "autocut_kernel"
KERNEL_BUILD_EVIDENCE_POLICY_PATH = "governance/kernel-build-evidence-policy.yaml"
KERNEL_DISTRIBUTION_NAME = "autocut-kernel"
AUTHORITY_SYNC_TASK_ID = "08-21-00-trellis-authority-sync"
PACKAGE_SKELETON_TASK_ID = "08-21-02-import-firewall-and-package-skeleton"
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
_VERIFIED_EVIDENCE_SEAL = object()


@dataclass(frozen=True, slots=True)
class _KernelBuildEvidenceInputs:
    authority_repository_root: Path
    authority_governance_commit: str
    kernel_repository_root: Path
    kernel_source_commit: str
    wheel_path: Path
    distribution_version: str
    build_recipe_path: Path
    environment_lock_path: Path
    provenance_receipt_path: Path


@dataclass(frozen=True, slots=True)
class _VerifiedKernelBuildEvidence:
    inputs: _KernelBuildEvidenceInputs
    receipt_json: bytes
    seal: object


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


def _read_regular_evidence_file(path: Path, *, where: str) -> bytes:
    if path.is_symlink():
        raise GateViolation("AUTH-KERNEL-EVIDENCE-SYMLINK", f"{where} cannot be a symlink")
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise GateViolation("AUTH-KERNEL-EVIDENCE-FILE", f"{where} is not a file")
        return resolved.read_bytes()
    except OSError as exc:
        raise GateViolation("AUTH-KERNEL-EVIDENCE-FILE", f"cannot read {where}: {exc}") from exc


def compute_kernel_source_subtree_hash(root: Path, commit: str) -> str:
    """Return the canonical content hash Phase 02 records in build provenance."""
    require_git_object_oid(root, commit, object_type="commit", where="kernel_source_commit")
    output = git_output(
        root,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        KERNEL_SOURCE_SUBTREE_PATH,
    )
    records = [record for record in output.split("\0") if record]
    if not records:
        raise GateViolation(
            "AUTH-KERNEL-SUBTREE-MISSING",
            f"{KERNEL_SOURCE_SUBTREE_PATH} is absent from kernel source commit",
        )
    entries: list[dict[str, str]] = []
    for record in records:
        try:
            metadata, relative = record.split("\t", 1)
            mode, object_type, _object_oid = metadata.split(" ", 2)
        except ValueError as exc:
            raise GateViolation("AUTH-KERNEL-SUBTREE-LIST", "invalid Git tree record") from exc
        path = validate_relative_path(relative, where="kernel subtree path")
        if not path.startswith(f"{KERNEL_SOURCE_SUBTREE_PATH}/"):
            raise GateViolation("AUTH-KERNEL-SUBTREE-PATH", "Git tree escaped kernel subtree")
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise GateViolation(
                "AUTH-KERNEL-SUBTREE-TYPE", f"unsupported kernel tree entry: {path}"
            )
        entries.append(
            {
                "path": path,
                "mode": mode,
                "sha256": sha256_bytes(git_bytes(root, commit, path)),
            }
        )
    return canonical_hash(entries)


def _committed_kernel_package_files(root: Path, commit: str) -> dict[str, bytes]:
    output = git_output(
        root,
        "ls-tree",
        "-r",
        "-z",
        commit,
        "--",
        KERNEL_PACKAGE_SOURCE_PATH,
    )
    records = [record for record in output.split("\0") if record]
    if not records:
        raise GateViolation(
            "AUTH-KERNEL-PACKAGE-MISSING", "committed kernel package source is empty"
        )
    package: dict[str, bytes] = {}
    for record in records:
        try:
            metadata, path = record.split("\t", 1)
            mode, object_type, _object_oid = metadata.split(" ", 2)
        except ValueError as exc:
            raise GateViolation("AUTH-KERNEL-SUBTREE-LIST", "invalid package tree record") from exc
        if object_type != "blob" or mode not in {"100644", "100755"}:
            raise GateViolation("AUTH-KERNEL-SUBTREE-TYPE", "package contains non-regular input")
        prefix = f"{KERNEL_PACKAGE_SOURCE_PATH}/"
        if not path.startswith(prefix):
            raise GateViolation("AUTH-KERNEL-SUBTREE-PATH", "package source escaped its root")
        relative = validate_relative_path(path[len(prefix) :], where="kernel package file")
        if relative.endswith((".pyc", ".pyo")) or "__pycache__" in relative.split("/"):
            raise GateViolation("AUTH-KERNEL-PACKAGE-GENERATED", "generated Python file in source")
        package[relative] = git_bytes(root, commit, path)
    return package


def _normalized_wheel_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9.]+", "_", value)


def _verify_wheel(
    *,
    wheel_path: Path,
    distribution_version: str,
    committed_package_files: Mapping[str, bytes],
) -> tuple[bytes, str, str]:
    wheel_raw = _read_regular_evidence_file(wheel_path, where="wheel")
    filename = wheel_path.name
    if not filename.endswith(".whl"):
        raise GateViolation("AUTH-KERNEL-WHEEL-NAME", "wheel must end in .whl")
    parts = filename[:-4].rsplit("-", 3)
    if len(parts) != 4:
        raise GateViolation("AUTH-KERNEL-WHEEL-NAME", "wheel filename has no canonical tag")
    prefix, python_tag, abi_tag, platform_tag = parts
    wheel_tag = f"{python_tag}-{abi_tag}-{platform_tag}"
    expected_prefix = (
        f"{_normalized_wheel_component(KERNEL_DISTRIBUTION_NAME)}-"
        f"{_normalized_wheel_component(distribution_version)}"
    )
    if prefix != expected_prefix:
        raise GateViolation(
            "AUTH-KERNEL-WHEEL-NAME", "wheel filename differs from distribution/version"
        )

    dist_info = f"{expected_prefix}.dist-info"
    try:
        with zipfile.ZipFile(io.BytesIO(wheel_raw)) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)) or archive.testzip() is not None:
                raise GateViolation("AUTH-KERNEL-WHEEL-ARCHIVE", "wheel archive is ambiguous")
            for name in names:
                if name.endswith("/"):
                    continue
                pure = PurePosixPath(name)
                if (
                    name.startswith("/")
                    or "\\" in name
                    or any(part in {"", ".", ".."} for part in pure.parts)
                ):
                    raise GateViolation(
                        "AUTH-KERNEL-WHEEL-ARCHIVE", "wheel contains a non-canonical path"
                    )
            required = {
                f"{dist_info}/WHEEL",
                f"{dist_info}/METADATA",
                f"{dist_info}/RECORD",
            }
            if not required.issubset(names):
                raise GateViolation(
                    "AUTH-KERNEL-WHEEL-ARCHIVE", "wheel metadata files are incomplete"
                )
            wheel_metadata = archive.read(f"{dist_info}/WHEEL").decode("utf-8")
            package_metadata = archive.read(f"{dist_info}/METADATA").decode("utf-8")
            archived_files = {name: archive.read(name) for name in names if not name.endswith("/")}
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise GateViolation("AUTH-KERNEL-WHEEL-ARCHIVE", f"invalid wheel archive: {exc}") from exc
    tags = {
        line.partition(":")[2].strip()
        for line in wheel_metadata.splitlines()
        if line.startswith("Tag:")
    }
    if wheel_tag not in tags:
        raise GateViolation("AUTH-KERNEL-WHEEL-TAG", "filename tag is absent from WHEEL metadata")
    metadata = {
        name.strip(): value.strip()
        for line in package_metadata.splitlines()
        if ":" in line
        for name, _, value in (line.partition(":"),)
    }
    if (
        _normalized_wheel_component(metadata.get("Name", ""))
        != _normalized_wheel_component(KERNEL_DISTRIBUTION_NAME)
        or metadata.get("Version") != distribution_version
    ):
        raise GateViolation(
            "AUTH-KERNEL-WHEEL-METADATA", "wheel METADATA differs from distribution/version"
        )
    wheel_package_prefix = f"{WHEEL_PACKAGE_PATH}/"
    wheel_package = {
        name[len(wheel_package_prefix) :]: raw
        for name, raw in archived_files.items()
        if name.startswith(wheel_package_prefix)
    }
    if wheel_package != dict(committed_package_files):
        raise GateViolation(
            "AUTH-KERNEL-WHEEL-SOURCE-MISMATCH",
            "wheel package files are not byte-identical to committed kernel source",
        )
    expected_archive_paths = required | {
        f"{wheel_package_prefix}{relative}" for relative in committed_package_files
    }
    if set(archived_files) != expected_archive_paths:
        raise GateViolation(
            "AUTH-KERNEL-WHEEL-EXTRA",
            "wheel contains files outside the committed package and required metadata",
        )

    record_path = f"{dist_info}/RECORD"
    try:
        rows = list(csv.reader(archived_files[record_path].decode("utf-8").splitlines()))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise GateViolation("AUTH-KERNEL-WHEEL-RECORD", f"invalid RECORD: {exc}") from exc
    record_entries: dict[str, tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3 or row[0] in record_entries:
            raise GateViolation("AUTH-KERNEL-WHEEL-RECORD", "RECORD rows are not canonical")
        record_entries[row[0]] = (row[1], row[2])
    if set(record_entries) != set(archived_files):
        raise GateViolation("AUTH-KERNEL-WHEEL-RECORD", "RECORD does not cover every wheel file")
    for name, raw in archived_files.items():
        recorded_hash, recorded_size = record_entries[name]
        if name == record_path:
            if recorded_hash or recorded_size:
                raise GateViolation("AUTH-KERNEL-WHEEL-RECORD", "RECORD self-row must be empty")
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).rstrip(b"=").decode()
        if recorded_hash != f"sha256={digest}" or recorded_size != str(len(raw)):
            raise GateViolation("AUTH-KERNEL-WHEEL-RECORD", f"RECORD hash/size mismatch for {name}")
    return wheel_raw, filename, wheel_tag


def _validate_provenance_receipt(
    raw: bytes,
    *,
    kernel_source_commit: str,
    kernel_source_subtree_hash: str,
    wheel_sha256: str,
    build_recipe_hash: str,
    environment_lock_hash: str,
    build_evidence_policy_hash: str,
    approved_builder_ids: set[str],
) -> None:
    provenance = load_mapping_bytes(raw, where="kernel build provenance", suffix=".json")
    require_closed(
        provenance,
        required=(
            "schema_version",
            "receipt_type",
            "builder_id",
            "decision",
            "build_evidence_policy_hash",
            "kernel_source_commit",
            "kernel_source_subtree_hash",
            "wheel_sha256",
            "build_recipe_hash",
            "environment_lock_hash",
        ),
        where="kernel build provenance",
    )
    if (
        provenance["schema_version"] != "1.0.0"
        or provenance["receipt_type"] != "kernel_build_provenance"
        or provenance["decision"] != "allow"
    ):
        raise GateViolation("AUTH-KERNEL-PROVENANCE", "provenance status/type is invalid")
    builder_id = require_non_empty_string(provenance["builder_id"], where="provenance.builder_id")
    if builder_id not in approved_builder_ids:
        raise GateViolation("AUTH-KERNEL-BUILDER", "builder is not authority-approved")
    expected = {
        "build_evidence_policy_hash": build_evidence_policy_hash,
        "kernel_source_commit": kernel_source_commit,
        "kernel_source_subtree_hash": kernel_source_subtree_hash,
        "wheel_sha256": wheel_sha256,
        "build_recipe_hash": build_recipe_hash,
        "environment_lock_hash": environment_lock_hash,
    }
    if any(provenance[field] != value for field, value in expected.items()):
        raise GateViolation("AUTH-KERNEL-PROVENANCE", "provenance evidence does not match build")


def _validate_kernel_build_evidence_policy(policy: Mapping[str, Any]) -> tuple[set[str], set[str]]:
    require_closed(
        policy,
        required=(
            "schema_version",
            "policy_id",
            "kernel_repository",
            "authority_lock_path",
            "kernel_source_subtree_path",
            "kernel_package_source_path",
            "wheel_package_path",
            "distribution_name",
            "approved_builder_ids",
            "allowed_wheel_tags",
            "require_authority_ancestor",
        ),
        where="kernel build evidence policy",
    )
    constants = {
        "schema_version": "1.0.0",
        "policy_id": "autocut-kernel-isolated-wheel-v1",
        "kernel_repository": "auto_cut_bot",
        "authority_lock_path": AUTHORITY_LOCK_PATH,
        "kernel_source_subtree_path": KERNEL_SOURCE_SUBTREE_PATH,
        "kernel_package_source_path": KERNEL_PACKAGE_SOURCE_PATH,
        "wheel_package_path": WHEEL_PACKAGE_PATH,
        "distribution_name": KERNEL_DISTRIBUTION_NAME,
        "require_authority_ancestor": True,
    }
    if any(policy[field] != expected for field, expected in constants.items()):
        raise GateViolation("AUTH-KERNEL-EVIDENCE-POLICY", "policy constants are invalid")
    builders = set(
        _require_unique_strings(policy["approved_builder_ids"], where="approved_builder_ids")
    )
    tags = set(_require_unique_strings(policy["allowed_wheel_tags"], where="allowed_wheel_tags"))
    return builders, tags


def _derive_kernel_build_receipt(inputs: _KernelBuildEvidenceInputs) -> dict[str, Any]:
    authority_root = inputs.authority_repository_root.resolve(strict=True)
    kernel_root = inputs.kernel_repository_root.resolve(strict=True)
    require_git_object_oid(
        authority_root,
        inputs.authority_governance_commit,
        object_type="commit",
        where="authority_governance_commit",
    )
    authority_raw = git_bytes(
        authority_root,
        inputs.authority_governance_commit,
        AUTHORITY_LOCK_PATH,
    )
    authority_lock = load_mapping_bytes(authority_raw, where="committed authority lock")
    validate_authority_lock(authority_lock)
    policy_raw = git_bytes(
        authority_root,
        inputs.authority_governance_commit,
        KERNEL_BUILD_EVIDENCE_POLICY_PATH,
    )
    policy = load_mapping_bytes(policy_raw, where="committed kernel build evidence policy")
    approved_builder_ids, allowed_wheel_tags = _validate_kernel_build_evidence_policy(policy)
    matching_policy_entries = [
        entry
        for entry in authority_lock["entries"]
        if entry["repository"] == "auto_cut_bot"
        and entry["path"] == KERNEL_BUILD_EVIDENCE_POLICY_PATH
    ]
    if len(matching_policy_entries) != 1 or matching_policy_entries[0]["sha256"] != sha256_bytes(
        policy_raw
    ):
        raise GateViolation(
            "AUTH-KERNEL-EVIDENCE-POLICY-UNLOCKED",
            "kernel evidence policy is not bound by the committed authority lock",
        )
    if authority_root != kernel_root:
        raise GateViolation(
            "AUTH-KERNEL-REPOSITORY", "authority and kernel source must use auto_cut_bot"
        )
    try:
        git_output(
            kernel_root,
            "merge-base",
            "--is-ancestor",
            inputs.authority_governance_commit,
            inputs.kernel_source_commit,
        )
    except GateViolation as exc:
        raise GateViolation(
            "AUTH-KERNEL-SOURCE-LINEAGE",
            "kernel source commit must descend from authority governance commit",
        ) from exc
    kernel_source_subtree_hash = compute_kernel_source_subtree_hash(
        kernel_root, inputs.kernel_source_commit
    )
    committed_package_files = _committed_kernel_package_files(
        kernel_root, inputs.kernel_source_commit
    )
    distribution_version = require_non_empty_string(
        inputs.distribution_version, where="distribution_version"
    )
    wheel_raw, wheel_filename, wheel_tag = _verify_wheel(
        wheel_path=inputs.wheel_path,
        distribution_version=distribution_version,
        committed_package_files=committed_package_files,
    )
    if wheel_tag not in allowed_wheel_tags:
        raise GateViolation("AUTH-KERNEL-WHEEL-TAG", "wheel tag is not authority-approved")
    build_recipe_raw = _read_regular_evidence_file(inputs.build_recipe_path, where="build recipe")
    environment_lock_raw = _read_regular_evidence_file(
        inputs.environment_lock_path, where="environment lock"
    )
    provenance_raw = _read_regular_evidence_file(
        inputs.provenance_receipt_path, where="provenance receipt"
    )
    if not build_recipe_raw or not environment_lock_raw or not provenance_raw:
        raise GateViolation("AUTH-KERNEL-EVIDENCE-EMPTY", "build evidence cannot be empty")
    wheel_sha256 = sha256_bytes(wheel_raw)
    build_recipe_hash = sha256_bytes(build_recipe_raw)
    environment_lock_hash = sha256_bytes(environment_lock_raw)
    _validate_provenance_receipt(
        provenance_raw,
        kernel_source_commit=inputs.kernel_source_commit,
        kernel_source_subtree_hash=kernel_source_subtree_hash,
        wheel_sha256=wheel_sha256,
        build_recipe_hash=build_recipe_hash,
        environment_lock_hash=environment_lock_hash,
        build_evidence_policy_hash=sha256_bytes(policy_raw),
        approved_builder_ids=approved_builder_ids,
    )
    authority_bundle_hash = require_sha256(
        authority_lock["bundle_hash"], where="authority bundle hash"
    )
    return make_typed_receipt(
        "kernel_build",
        authority_lock_hash=authority_bundle_hash,
        decision="allow",
        reason_codes=[],
        task_id=PACKAGE_SKELETON_TASK_ID,
        authority_governance_commit=inputs.authority_governance_commit,
        authority_lock_document_hash=sha256_bytes(authority_raw),
        authority_bundle_hash=authority_bundle_hash,
        build_evidence_policy_hash=sha256_bytes(policy_raw),
        kernel_source_commit=inputs.kernel_source_commit,
        kernel_source_subtree_hash=kernel_source_subtree_hash,
        distribution_name=KERNEL_DISTRIBUTION_NAME,
        distribution_version=distribution_version,
        wheel_filename=wheel_filename,
        wheel_tag=wheel_tag,
        wheel_size_bytes=len(wheel_raw),
        wheel_sha256=wheel_sha256,
        build_recipe_hash=build_recipe_hash,
        environment_lock_hash=environment_lock_hash,
        provenance_receipt_hash=sha256_bytes(provenance_raw),
    )


def verify_kernel_build_evidence(
    *,
    authority_repository_root: Path,
    authority_governance_commit: str,
    kernel_repository_root: Path,
    kernel_source_commit: str,
    wheel_path: Path,
    distribution_version: str,
    build_recipe_path: Path,
    environment_lock_path: Path,
    provenance_receipt_path: Path,
) -> _VerifiedKernelBuildEvidence:
    """Replay exact immutable/build evidence and return a sealed capability."""

    inputs = _KernelBuildEvidenceInputs(
        authority_repository_root=authority_repository_root.resolve(strict=True),
        authority_governance_commit=authority_governance_commit,
        kernel_repository_root=kernel_repository_root.resolve(strict=True),
        kernel_source_commit=kernel_source_commit,
        wheel_path=wheel_path.resolve(strict=True),
        distribution_version=distribution_version,
        build_recipe_path=build_recipe_path.resolve(strict=True),
        environment_lock_path=environment_lock_path.resolve(strict=True),
        provenance_receipt_path=provenance_receipt_path.resolve(strict=True),
    )
    receipt = _derive_kernel_build_receipt(inputs)
    return _VerifiedKernelBuildEvidence(
        inputs=inputs,
        receipt_json=json.dumps(receipt, sort_keys=True, separators=(",", ":")).encode(),
        seal=_VERIFIED_EVIDENCE_SEAL,
    )


def _replay_verified_kernel_build_evidence(
    evidence: object,
) -> dict[str, Any]:
    if not isinstance(evidence, _VerifiedKernelBuildEvidence) or (
        evidence.seal is not _VERIFIED_EVIDENCE_SEAL
    ):
        raise GateViolation("AUTH-KERNEL-EVIDENCE-UNVERIFIED", "kernel evidence is not sealed")
    stored = load_mapping_bytes(
        evidence.receipt_json, where="sealed kernel receipt", suffix=".json"
    )
    validate_typed_receipt(stored, expected_type="kernel_build")
    replayed = _derive_kernel_build_receipt(evidence.inputs)
    if replayed["receipt_id"] != stored["receipt_id"]:
        raise GateViolation("AUTH-KERNEL-EVIDENCE-STALE", "kernel build evidence changed")
    return replayed


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


def validate_authority_consumer_lock_structure(
    lock: Mapping[str, Any],
    *,
    profile_policy: Mapping[str, Any],
) -> None:
    """Validate closed shape only; this does not grant authority-valid semantics."""

    validate_consumer_lock_policy(profile_policy)
    require_closed(
        lock,
        required=(
            "schema_version",
            "contract_version",
            "authority_governance_commit",
            "authority_lock_document_hash",
            "authority_bundle_hash",
            "build_evidence_policy_hash",
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
        "build_evidence_policy_hash",
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


def validate_authority_consumer_lock(
    lock: Mapping[str, Any],
    *,
    profile_policy: Mapping[str, Any],
    kernel_build_evidence: object,
) -> None:
    """Validate structure and independently replay every kernel build input."""

    validate_authority_consumer_lock_structure(lock, profile_policy=profile_policy)
    kernel_build_receipt = _replay_verified_kernel_build_evidence(kernel_build_evidence)
    validate_typed_receipt(kernel_build_receipt, expected_type="kernel_build")
    if kernel_build_receipt["decision"] != "allow":
        raise GateViolation("AUTH-CONSUMER-KERNEL-DENY", "kernel build receipt must allow")
    if kernel_build_receipt["authority_lock_hash"] != lock["authority_bundle_hash"]:
        raise GateViolation("AUTH-CONSUMER-AUTHORITY-HASH", "receipt authority hash mismatch")
    comparisons = {
        "authority_governance_commit": "authority_governance_commit",
        "authority_lock_document_hash": "authority_lock_document_hash",
        "authority_bundle_hash": "authority_bundle_hash",
        "build_evidence_policy_hash": "build_evidence_policy_hash",
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
    observed = {
        str(item["receipt_type"]): str(item["receipt_hash"])
        for item in cast(list[dict[str, str]], lock["materialization_receipts"])
    }
    if observed.get("KernelBuildReceipt") != kernel_build_receipt["receipt_id"]:
        raise GateViolation("AUTH-CONSUMER-KERNEL-MISMATCH", "kernel receipt hash mismatch")


def build_authority_consumer_lock(
    *,
    kernel_build_evidence: object,
    eligibility_profile: str,
    profile_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Return deterministic lock bytes as data; this function never writes a file."""

    kernel_build_receipt = _replay_verified_kernel_build_evidence(kernel_build_evidence)
    validate_consumer_lock_policy(profile_policy)
    if eligibility_profile != "bootstrap_consumable":
        raise GateViolation(
            "AUTH-CONSUMER-PROFILE-NOT-ACTIVE",
            "Phase 02 may only materialize bootstrap_consumable",
        )
    materialization_receipts = [
        {
            "receipt_type": "KernelBuildReceipt",
            "receipt_hash": kernel_build_receipt["receipt_id"],
        }
    ]
    lock = {
        "schema_version": "1.0.0",
        "contract_version": "2.1.3",
        "authority_governance_commit": kernel_build_receipt["authority_governance_commit"],
        "authority_lock_document_hash": kernel_build_receipt["authority_lock_document_hash"],
        "authority_bundle_hash": kernel_build_receipt["authority_bundle_hash"],
        "build_evidence_policy_hash": kernel_build_receipt["build_evidence_policy_hash"],
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
        lock, profile_policy=profile_policy, kernel_build_evidence=kernel_build_evidence
    )
    return lock


def assert_phase00_consumer_lock_absent(
    *, consumer_repository_root: Path, consumer_repository_commit: str
) -> tuple[str, str]:
    """Prove the reserved lock is absent from commit, index and worktree."""

    root = consumer_repository_root.resolve(strict=True)
    require_git_object_oid(
        root,
        consumer_repository_commit,
        object_type="commit",
        where="consumer_repository_commit",
    )
    commit_tree_oid = git_output(root, "rev-parse", f"{consumer_repository_commit}^{{tree}}")
    require_git_object_oid(root, commit_tree_oid, object_type="tree", where="consumer commit tree")
    if git_output(
        root,
        "ls-tree",
        "-r",
        "--name-only",
        consumer_repository_commit,
        "--",
        CONSUMER_LOCK_PATH,
    ):
        raise GateViolation(
            "AUTH-CONSUMER-LOCK-COMMITTED",
            "consumer commit tree contains the reserved consumer lock",
        )
    index_tree_oid = git_output(root, "write-tree")
    require_git_object_oid(root, index_tree_oid, object_type="tree", where="consumer index tree")
    if git_output(
        root,
        "ls-tree",
        "-r",
        "--name-only",
        index_tree_oid,
        "--",
        CONSUMER_LOCK_PATH,
    ):
        raise GateViolation(
            "AUTH-CONSUMER-LOCK-INDEXED",
            "consumer Git index contains the reserved consumer lock",
        )
    target = contained_path(root, CONSUMER_LOCK_PATH)
    if target.exists() or target.is_symlink():
        raise GateViolation(
            "AUTH-CONSUMER-LOCK-WORKTREE",
            "consumer worktree contains the reserved consumer lock",
        )
    return commit_tree_oid, index_tree_oid


def make_consumer_lock_readiness_receipt(
    *,
    task_id: str,
    authority_governance_commit: str,
    authority_bundle_hash: str,
    consumer_repository_root: Path,
    consumer_repository_commit: str,
    profile_policy: Mapping[str, Any],
) -> dict[str, Any]:
    """Issue the only legal Phase-00 consumer-lock predicate."""

    if task_id != AUTHORITY_SYNC_TASK_ID:
        raise GateViolation(
            "AUTH-CONSUMER-READINESS-TASK",
            "only child 00 may issue the not-materialized readiness receipt",
        )
    validate_consumer_lock_policy(profile_policy)
    consumer_commit_tree_oid, consumer_index_tree_oid = assert_phase00_consumer_lock_absent(
        consumer_repository_root=consumer_repository_root,
        consumer_repository_commit=consumer_repository_commit,
    )
    receipt = make_typed_receipt(
        "consumer_lock_readiness",
        authority_lock_hash=authority_bundle_hash,
        decision="not_applicable",
        reason_codes=[],
        task_id=task_id,
        authority_governance_commit=authority_governance_commit,
        authority_bundle_hash=authority_bundle_hash,
        consumer_repository_commit=consumer_repository_commit,
        consumer_commit_tree_oid=consumer_commit_tree_oid,
        consumer_index_tree_oid=consumer_index_tree_oid,
        consumer_lock_path=CONSUMER_LOCK_PATH,
        state="not_materialized",
        reason=READINESS_REASON,
        profile_policy_hash=canonical_hash(profile_policy),
    )
    verify_consumer_lock_readiness_receipt(
        receipt=receipt,
        consumer_repository_root=consumer_repository_root,
        profile_policy=profile_policy,
        expected_authority_governance_commit=authority_governance_commit,
        expected_authority_bundle_hash=authority_bundle_hash,
    )
    return receipt


def verify_consumer_lock_readiness_receipt(
    *,
    receipt: Mapping[str, Any],
    consumer_repository_root: Path,
    profile_policy: Mapping[str, Any],
    expected_authority_governance_commit: str,
    expected_authority_bundle_hash: str,
) -> None:
    """Replay a readiness receipt against trusted authority inputs and live Git state."""

    validate_typed_receipt(receipt, expected_type="consumer_lock_readiness")
    if receipt["task_id"] != AUTHORITY_SYNC_TASK_ID:
        raise GateViolation(
            "AUTH-CONSUMER-READINESS-TASK", "readiness receipt has a non-canonical task ID"
        )
    expected = {
        "authority_governance_commit": expected_authority_governance_commit,
        "authority_bundle_hash": expected_authority_bundle_hash,
        "authority_lock_hash": expected_authority_bundle_hash,
        "profile_policy_hash": canonical_hash(profile_policy),
        "consumer_lock_path": CONSUMER_LOCK_PATH,
        "state": "not_materialized",
        "reason": READINESS_REASON,
    }
    if any(receipt[field] != value for field, value in expected.items()):
        raise GateViolation(
            "AUTH-CONSUMER-READINESS-MISMATCH",
            "readiness receipt differs from trusted authority/profile inputs",
        )
    commit_tree_oid, index_tree_oid = assert_phase00_consumer_lock_absent(
        consumer_repository_root=consumer_repository_root,
        consumer_repository_commit=str(receipt["consumer_repository_commit"]),
    )
    if (
        receipt["consumer_commit_tree_oid"] != commit_tree_oid
        or receipt["consumer_index_tree_oid"] != index_tree_oid
    ):
        raise GateViolation(
            "AUTH-CONSUMER-READINESS-MISMATCH",
            "readiness receipt does not match replayed commit/index trees",
        )


def make_committed_consumer_lock_receipt(
    *,
    task_id: str,
    consumer_repository_root: Path,
    consumer_repository_commit: str,
    profile_policy: Mapping[str, Any],
    kernel_build_evidence: object,
) -> dict[str, Any]:
    """Bind a committed lock blob and consumer tree without lock self-reference."""

    raw = git_bytes(consumer_repository_root, consumer_repository_commit, CONSUMER_LOCK_PATH)
    lock = load_mapping_bytes(raw, where=f"{consumer_repository_commit}:{CONSUMER_LOCK_PATH}")
    validate_authority_consumer_lock(
        lock,
        profile_policy=profile_policy,
        kernel_build_evidence=kernel_build_evidence,
    )
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
    kernel_build_evidence: object,
) -> None:
    """Replay a post-commit receipt from immutable Git objects."""

    validate_typed_receipt(receipt, expected_type="consumer_lock")
    if receipt["decision"] != "allow":
        raise GateViolation("AUTH-CONSUMER-RECEIPT-DENY", "consumer lock receipt did not allow")
    commit = str(receipt["consumer_repository_commit"])
    raw = git_bytes(consumer_repository_root, commit, CONSUMER_LOCK_PATH)
    committed_lock = load_mapping_bytes(raw, where=f"{commit}:{CONSUMER_LOCK_PATH}")
    validate_authority_consumer_lock(
        committed_lock,
        profile_policy=profile_policy,
        kernel_build_evidence=kernel_build_evidence,
    )
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
    kernel_build_evidence: object,
    consumer_repository_root: Path | None = None,
) -> None:
    """Enforce receipt closure and the selected profile's capability ceiling."""

    validate_authority_consumer_lock(
        lock,
        profile_policy=profile_policy,
        kernel_build_evidence=kernel_build_evidence,
    )
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
                kernel_build_evidence=kernel_build_evidence,
            )
    required = set(profile["post_commit_required_receipt_types"])
    if not required.issubset(observed_types):
        raise GateViolation(
            "AUTH-CONSUMER-RECEIPT-MISSING",
            f"missing post-commit receipts: {sorted(required - observed_types)}",
        )
