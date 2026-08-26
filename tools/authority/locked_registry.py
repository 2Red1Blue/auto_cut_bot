"""Build a Registry only from an explicitly verified immutable A/B/C lock.

This build-time adapter never consumes checkout/index bytes or installs a
runtime authority resource. A ready Registry is not calibration acceptance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from autocut_kernel.contracts.compiler.registry import RegistrySet
from autocut_kernel.contracts.compiler.registry_source import (
    compile_registry_source,
    load_registry_source_manifest,
)

from .common import (
    git_bytes,
    git_output,
    load_mapping_bytes,
    require_commit,
    sha256_bytes,
    validate_relative_path,
)
from .errors import GateViolation
from .lock import (
    validate_authority_lock,
    verify_authority_lock_data,
    verify_bootstrap_commit_chain,
)


@dataclass(frozen=True, slots=True)
class LockedRegistryCompilation:
    registry_set: RegistrySet
    lock_raw: bytes
    lock_repository: str
    lock_commit: str
    lock_path: str
    registry_repository: str
    registry_root: str


def _regular_git_blob(root: Path, commit: str, path: str) -> bytes:
    require_commit(commit, where="source commit")
    validate_relative_path(path, where="source path")
    rows = git_output(root, "ls-tree", "-z", commit, "--", path).split("\0")
    entries = [row.partition("\t") for row in rows if row]
    if len(entries) != 1 or entries[0][2] != path:
        raise GateViolation("AUTH-REGISTRY-BLOB", f"not an exact Git entry: {path}")
    metadata = entries[0][0].split()
    if len(metadata) != 3 or metadata[:2] not in (["100644", "blob"], ["100755", "blob"]):
        raise GateViolation("AUTH-REGISTRY-BLOB", f"not a regular Git blob: {path}")
    return git_bytes(root, commit, path)


def read_locked_blob(
    *,
    lock: Mapping[str, Any],
    repository_roots: Mapping[str, Path],
    repository: str,
    path: str,
    expected_class: str,
) -> bytes:
    """Check one lock entry's class, regular Git mode and raw digest.

    This helper does NOT verify the authority commit chain; callers must use a
    lock obtained from ``compile_locked_registry`` before treating it as one.
    """
    validate_authority_lock(lock)
    validate_relative_path(path, where="locked blob path")
    if repository not in repository_roots or repository not in lock["repositories"]:
        raise GateViolation("AUTH-REGISTRY-REPOSITORY", "locked blob repository is unbound")
    entries = [entry for entry in lock["entries"] if entry["repository"] == repository and entry["path"] == path]
    if len(entries) != 1 or entries[0]["class"] != expected_class:
        raise GateViolation("AUTH-REGISTRY-ENTRY", f"missing exact {expected_class} entry: {repository}:{path}")
    raw = _regular_git_blob(repository_roots[repository], lock["repositories"][repository]["source_commit"], path)
    if sha256_bytes(raw) != entries[0]["sha256"]:
        raise GateViolation("AUTH-LOCK-FILE-HASH", f"hash mismatch for {repository}:{path}")
    return raw


def compile_locked_registry(
    *,
    repository_roots: Mapping[str, Path],
    lock_repository: str,
    lock_commit: str,
    lock_path: str,
    registry_repository: str,
    registry_root: str,
) -> LockedRegistryCompilation:
    """Verify C→B→A, materialize exactly the locked Registry, and require ready."""
    validate_relative_path(lock_path, where="lock_path")
    validate_relative_path(registry_root, where="registry_root")
    if lock_repository not in repository_roots or registry_repository not in repository_roots:
        raise GateViolation("AUTH-REGISTRY-REPOSITORY", "selected repository is unbound")
    lock_raw = _regular_git_blob(repository_roots[lock_repository], lock_commit, lock_path)
    lock = load_mapping_bytes(lock_raw, where=f"{lock_commit}:{lock_path}", suffix=Path(lock_path).suffix)
    validate_authority_lock(lock)
    inventory = lock["inventory"]
    if inventory["repository"] != lock_repository:
        raise GateViolation("AUTH-REGISTRY-LOCK-OWNER", "A/B/C must belong to the inventory repository")
    verify_bootstrap_commit_chain(
        repository_root=repository_roots[lock_repository],
        seed_commit=lock["seed_source_commit"], inventory_commit=inventory["manifest_commit"],
        lock_commit=lock_commit, source_manifest_repository=inventory["repository"],
        source_manifest_path=inventory["path"], generated_lock_path=lock_path,
        repository_roots=repository_roots,
    )
    verify_authority_lock_data(lock, repository_roots)
    prefix = registry_root + "/"
    selected = [entry for entry in lock["entries"] if entry["repository"] == registry_repository and entry["path"].startswith(prefix)]
    if not selected or any(entry["class"] != "registry_source" for entry in selected):
        raise GateViolation("AUTH-REGISTRY-ENTRY", "selected Registry must contain only registry_source entries")
    source_commit = lock["repositories"][registry_repository]["source_commit"]
    tree = git_output(repository_roots[registry_repository], "ls-tree", "-r", "-z", source_commit, "--", registry_root)
    tree_paths = {row.partition("\t")[2] for row in tree.split("\0") if row}
    if tree_paths != {entry["path"] for entry in selected}:
        raise GateViolation("AUTH-REGISTRY-COVERAGE", "Registry Git tree differs from locked file coverage")
    with TemporaryDirectory(prefix="autocut-locked-registry-") as temporary:
        root = Path(temporary)
        materialized: set[str] = set()
        for entry in selected:
            relative = entry["path"][len(prefix):]
            validate_relative_path(relative, where="Registry source path")
            raw = read_locked_blob(lock=lock, repository_roots=repository_roots,
                                   repository=registry_repository, path=entry["path"], expected_class="registry_source")
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            if sha256_bytes(target.read_bytes()) != entry["sha256"]:
                raise GateViolation("AUTH-REGISTRY-MATERIALIZATION", f"private source hash mismatch: {relative}")
            materialized.add(relative)
        manifest = load_registry_source_manifest(root)
        required = {"common/registry_set.yaml"}
        required.update(document.path for document in manifest.registry_documents)
        required.update(source.path for pack in manifest.source_packs for source in pack.source_paths)
        if required != materialized:
            raise GateViolation("AUTH-REGISTRY-COVERAGE", "manifest, documents and source paths must exhaust the locked Registry")
        registry_set = compile_registry_source(root)
        if not isinstance(registry_set, RegistrySet):
            raise GateViolation("AUTH-REGISTRY-COMPILER", "compiler did not return a RegistrySet")
        registry_set.require_ready()
    return LockedRegistryCompilation(registry_set, lock_raw, lock_repository, lock_commit,
                                     lock_path, registry_repository, registry_root)
