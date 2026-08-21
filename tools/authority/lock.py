# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false
"""Authority lock validation and byte-level verification."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .common import (
    canonical_hash,
    git_bytes,
    git_index_bytes,
    git_index_paths,
    git_output,
    load_mapping,
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

LOCK_CLASSES = {
    "production_contract",
    "implementation_contract",
    "schema_source",
    "registry_source",
    "architecture_gate",
    "blocking_fixture",
}


def build_authority_lock(
    *,
    source_manifest_repository: str,
    source_manifest_commit: str,
    source_manifest_path: str,
    repository_roots: Mapping[str, Path],
) -> dict[str, Any]:
    """Build a lock using only blobs reachable from reviewed commits."""

    if source_manifest_repository not in repository_roots:
        raise GateViolation("AUTH-SOURCE-REPOSITORY", "source manifest repository is unbound")
    require_commit(source_manifest_commit, where="source_manifest_commit")
    relative_manifest = validate_relative_path(source_manifest_path, where="source_manifest_path")
    source_manifest_raw = git_bytes(
        repository_roots[source_manifest_repository],
        source_manifest_commit,
        relative_manifest,
    )
    source = load_mapping_bytes(
        source_manifest_raw,
        where=f"{source_manifest_commit}:{relative_manifest}",
        suffix=Path(relative_manifest).suffix,
    )
    require_closed(
        source,
        required=(
            "schema_version",
            "authority_id",
            "authority_revision",
            "contract_version",
            "seed_source_commit",
            "repositories",
            "entries",
        ),
        where="authority source manifest",
    )
    if source["schema_version"] != "1.0.0":
        raise GateViolation("AUTH-SOURCE-VERSION", "unsupported authority source version")
    if not isinstance(source["repositories"], dict) or not source["repositories"]:
        raise GateViolation("AUTH-SOURCE-REPOSITORIES", "repositories must be a non-empty object")
    if set(repository_roots) != set(source["repositories"]):
        raise GateViolation("AUTH-SOURCE-ROOTS", "repository roots do not match source manifest")
    repositories: dict[str, dict[str, str]] = {}
    for name, repository in source["repositories"].items():
        if not isinstance(repository, dict):
            raise GateViolation("AUTH-SOURCE-REPOSITORY", f"repository {name} must be an object")
        require_closed(repository, required=("source_commit",), where=f"repository {name}")
        repositories[name] = {
            "source_commit": require_commit(
                repository["source_commit"], where=f"repository {name}.source_commit"
            )
        }
    seed_source_commit = require_commit(source["seed_source_commit"], where="seed_source_commit")
    if repositories[source_manifest_repository]["source_commit"] != seed_source_commit:
        raise GateViolation(
            "AUTH-SOURCE-SEED-MISMATCH",
            "seed_source_commit must equal the inventory repository source commit",
        )

    entries: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for index, item in enumerate(require_list(source["entries"], where="entries", non_empty=True)):
        if not isinstance(item, dict):
            raise GateViolation("AUTH-SOURCE-ENTRY", f"entries[{index}] must be an object")
        require_closed(item, required=("class", "repository", "path"), where=f"entries[{index}]")
        class_name = item["class"]
        if class_name not in LOCK_CLASSES:
            raise GateViolation("AUTH-LOCK-CLASS", f"unknown lock class: {class_name}")
        repository = require_non_empty_string(item["repository"], where="repository")
        if repository not in repositories:
            raise GateViolation("AUTH-SOURCE-REPOSITORY", f"unknown repository: {repository}")
        path = validate_relative_path(item["path"], where=f"entries[{index}].path")
        identity = (repository, path)
        if identity in seen:
            raise GateViolation("AUTH-SOURCE-DUPLICATE", f"duplicate source: {identity}")
        seen.add(identity)
        entries.append(
            {
                "class": class_name,
                "repository": repository,
                "path": path,
                "sha256": sha256_bytes(
                    git_bytes(
                        repository_roots[repository],
                        repositories[repository]["source_commit"],
                        path,
                    )
                ),
            }
        )
    entries.sort(key=lambda item: (item["repository"], item["path"], item["class"]))
    lock: dict[str, Any] = {
        "schema_version": source["schema_version"],
        "authority_id": require_non_empty_string(source["authority_id"], where="authority_id"),
        "authority_revision": source["authority_revision"],
        "contract_version": source["contract_version"],
        "seed_source_commit": seed_source_commit,
        "inventory": {
            "repository": source_manifest_repository,
            "manifest_commit": source_manifest_commit,
            "path": relative_manifest,
            "sha256": sha256_bytes(source_manifest_raw),
        },
        "repositories": repositories,
        "entries": entries,
    }
    lock["bundle_hash"] = canonical_hash(lock)
    validate_authority_lock(lock)
    return lock


def validate_authority_lock(lock: Mapping[str, Any]) -> None:
    require_closed(
        lock,
        required=(
            "schema_version",
            "authority_id",
            "authority_revision",
            "contract_version",
            "seed_source_commit",
            "inventory",
            "repositories",
            "entries",
            "bundle_hash",
        ),
        where="authority lock",
    )
    if lock["schema_version"] != "1.0.0" or lock["contract_version"] != "2.1.3":
        raise GateViolation("AUTH-LOCK-VERSION", "authority lock version is not supported")
    require_non_empty_string(lock["authority_id"], where="authority_id")
    if not isinstance(lock["authority_revision"], int) or lock["authority_revision"] < 1:
        raise GateViolation("AUTH-LOCK-REVISION", "authority_revision must be a positive integer")
    require_commit(lock["seed_source_commit"], where="seed_source_commit")
    inventory = lock["inventory"]
    if not isinstance(inventory, dict):
        raise GateViolation("AUTH-LOCK-INVENTORY", "inventory must be an object")
    require_closed(
        inventory,
        required=("repository", "manifest_commit", "path", "sha256"),
        where="inventory",
    )
    require_non_empty_string(inventory["repository"], where="inventory.repository")
    require_commit(inventory["manifest_commit"], where="inventory.manifest_commit")
    validate_relative_path(inventory["path"], where="inventory.path")
    require_sha256(inventory["sha256"], where="inventory.sha256")
    repositories = lock["repositories"]
    if not isinstance(repositories, dict) or not repositories:
        raise GateViolation("AUTH-LOCK-REPOSITORIES", "repositories must be a non-empty object")
    for name, repository in repositories.items():
        require_non_empty_string(name, where="repository name")
        if not isinstance(repository, dict):
            raise GateViolation("AUTH-LOCK-REPOSITORY", f"repository {name} must be an object")
        require_closed(repository, required=("source_commit",), where=f"repository {name}")
        require_commit(repository["source_commit"], where=f"repository {name}.source_commit")
    if inventory["repository"] not in repositories:
        raise GateViolation("AUTH-LOCK-INVENTORY", "inventory repository is not registered")
    if repositories[inventory["repository"]]["source_commit"] != lock["seed_source_commit"]:
        raise GateViolation(
            "AUTH-LOCK-SEED-MISMATCH", "seed commit differs from inventory repository"
        )

    seen: set[tuple[str, str]] = set()
    for index, entry in enumerate(require_list(lock["entries"], where="entries", non_empty=True)):
        if not isinstance(entry, dict):
            raise GateViolation("AUTH-LOCK-ENTRY", f"entries[{index}] must be an object")
        require_closed(
            entry,
            required=("class", "repository", "path", "sha256"),
            where=f"entries[{index}]",
        )
        if entry["class"] not in LOCK_CLASSES:
            raise GateViolation("AUTH-LOCK-CLASS", f"unknown lock class: {entry['class']}")
        repository_name = require_non_empty_string(
            entry["repository"], where=f"entries[{index}].repository"
        )
        if repository_name not in repositories:
            raise GateViolation("AUTH-LOCK-REPOSITORY", f"unknown repository {repository_name}")
        relative = validate_relative_path(entry["path"], where=f"entries[{index}].path")
        require_sha256(entry["sha256"], where=f"entries[{index}].sha256")
        identity = (repository_name, relative)
        if identity in seen:
            raise GateViolation("AUTH-LOCK-DUPLICATE", f"duplicate entry {identity}")
        seen.add(identity)

    require_sha256(lock["bundle_hash"], where="bundle_hash")
    hash_view = dict(lock)
    hash_view.pop("bundle_hash")
    if canonical_hash(hash_view) != lock["bundle_hash"]:
        raise GateViolation("AUTH-LOCK-BUNDLE-HASH", "bundle_hash does not match lock content")


def verify_authority_lock(lock_path: Path, repository_roots: Mapping[str, Path]) -> dict[str, str]:
    lock = load_mapping(lock_path)
    return verify_authority_lock_data(lock, repository_roots)


def verify_staged_authority_lock_candidate(
    *,
    lock_path: Path,
    repository_roots: Mapping[str, Path],
    predecessor_commits: Mapping[str, str],
) -> dict[str, str]:
    """Fail closed when an indexed phase-C candidate drifts from its lock.

    Plain lock verification intentionally reads immutable commits.  This
    companion check binds the *index* and therefore cannot approve a staged
    modification of a locked/governed source using the previous lock.
    """
    roots = {name: root.resolve(strict=True) for name, root in repository_roots.items()}
    if set(roots) != set(predecessor_commits):
        raise GateViolation("AUTH-LOCK-CANDIDATE-REPOSITORIES", "predecessor bindings mismatch")
    for name, commit in predecessor_commits.items():
        require_commit(commit, where=f"predecessor_commits[{name}]")
    owner: str | None = None
    relative: str | None = None
    for name, root in roots.items():
        try:
            relative = validate_relative_path(
                str(lock_path.resolve().relative_to(root)), where="authority lock path"
            )
            owner = name
            break
        except ValueError:
            continue
    if owner is None or relative is None:
        raise GateViolation("AUTH-LOCK-CANDIDATE-PATH", "lock is outside bound repositories")
    staged = load_mapping_bytes(
        git_index_bytes(roots[owner], relative), where=f"index:{relative}", suffix=lock_path.suffix
    )
    validate_authority_lock(staged)
    for name, root in roots.items():
        paths = git_index_paths(root, predecessor_commits[name])
        permitted = {relative} if name == owner else set()
        if set(paths) - permitted:
            raise GateViolation(
                "AUTH-LOCK-CANDIDATE-DRIFT",
                f"staged authority input changed without lock upgrade: {name}:{','.join(paths)}",
            )
    inventory = staged["inventory"]
    generated = build_authority_lock(
        source_manifest_repository=str(inventory["repository"]),
        source_manifest_commit=str(inventory["manifest_commit"]),
        source_manifest_path=str(inventory["path"]),
        repository_roots=roots,
    )
    if staged != generated:
        raise GateViolation("AUTH-LOCK-CANDIDATE-CONTENT", "staged lock is not regenerated")
    return verify_authority_lock_data(staged, roots)


def verify_authority_lock_data(
    lock: Mapping[str, Any], repository_roots: Mapping[str, Path]
) -> dict[str, str]:
    """Verify an already parsed lock against immutable Git blobs."""

    validate_authority_lock(lock)
    roots = {name: root.resolve(strict=True) for name, root in repository_roots.items()}
    if set(roots) != set(lock["repositories"]):
        raise GateViolation(
            "AUTH-LOCK-REPOSITORY-SET",
            "provided repository roots must exactly match authority lock repositories",
        )
    inventory = lock["inventory"]
    inventory_repository = str(inventory["repository"])
    if inventory_repository not in roots:
        raise GateViolation("AUTH-LOCK-INVENTORY", "inventory repository is unbound")
    inventory_actual = sha256_bytes(
        git_bytes(
            roots[inventory_repository],
            inventory["manifest_commit"],
            inventory["path"],
        )
    )
    if inventory_actual != inventory["sha256"]:
        raise GateViolation("AUTH-LOCK-INVENTORY-HASH", "inventory blob hash mismatch")
    verified: dict[str, str] = {"inventory": inventory_actual}
    for entry in lock["entries"]:
        identity = f"{entry['repository']}:{entry['path']}"
        actual = sha256_bytes(
            git_bytes(
                roots[entry["repository"]],
                lock["repositories"][entry["repository"]]["source_commit"],
                entry["path"],
            )
        )
        if actual != entry["sha256"]:
            raise GateViolation("AUTH-LOCK-FILE-HASH", f"hash mismatch for {identity}")
        verified[identity] = actual
    return verified


def verify_bootstrap_commit_chain(
    *,
    repository_root: Path,
    seed_commit: str,
    inventory_commit: str,
    lock_commit: str,
    source_manifest_repository: str,
    source_manifest_path: str,
    generated_lock_path: str,
    repository_roots: Mapping[str, Path],
) -> dict[str, str]:
    """Verify the non-self-referential A -> B -> C authority bootstrap."""

    for name, oid in (
        ("seed_commit", seed_commit),
        ("inventory_commit", inventory_commit),
        ("lock_commit", lock_commit),
    ):
        require_commit(oid, where=name)
    source_path = validate_relative_path(source_manifest_path, where="source_manifest_path")
    lock_path = validate_relative_path(generated_lock_path, where="generated_lock_path")

    inventory_parents = git_output(
        repository_root, "rev-list", "--parents", "-n", "1", inventory_commit
    ).split()
    if inventory_parents != [inventory_commit, seed_commit]:
        raise GateViolation("AUTH-BOOTSTRAP-INVENTORY-PARENT", "B must have exactly parent A")
    inventory_diff = git_output(
        repository_root, "diff", "--name-only", seed_commit, inventory_commit, "--"
    ).splitlines()
    if inventory_diff != [source_path]:
        raise GateViolation("AUTH-BOOTSTRAP-INVENTORY-DIFF", "B may change only inventory")

    lock_parents = git_output(
        repository_root, "rev-list", "--parents", "-n", "1", lock_commit
    ).split()
    if lock_parents != [lock_commit, inventory_commit]:
        raise GateViolation("AUTH-BOOTSTRAP-LOCK-PARENT", "C must have exactly parent B")
    lock_diff = git_output(
        repository_root, "diff", "--name-only", inventory_commit, lock_commit, "--"
    ).splitlines()
    if lock_diff != [lock_path]:
        raise GateViolation("AUTH-BOOTSTRAP-LOCK-DIFF", "C may change only generated lock")

    source_raw = git_bytes(repository_root, inventory_commit, source_path)
    source = load_mapping_bytes(source_raw, where=f"{inventory_commit}:{source_path}")
    if source.get("seed_source_commit") != seed_commit:
        raise GateViolation("AUTH-BOOTSTRAP-SEED", "inventory does not bind A")
    repositories = source.get("repositories")
    if not isinstance(repositories, dict):
        raise GateViolation("AUTH-BOOTSTRAP-REPOSITORIES", "inventory repositories invalid")
    inventory_repository = repositories.get(source_manifest_repository)
    if (
        not isinstance(inventory_repository, dict)
        or inventory_repository.get("source_commit") != seed_commit
    ):
        raise GateViolation(
            "AUTH-BOOTSTRAP-SEED", "inventory repository source_commit does not bind A"
        )
    generated = build_authority_lock(
        source_manifest_repository=source_manifest_repository,
        source_manifest_commit=inventory_commit,
        source_manifest_path=source_path,
        repository_roots=repository_roots,
    )
    committed_lock = load_mapping_bytes(
        git_bytes(repository_root, lock_commit, lock_path), where=f"{lock_commit}:{lock_path}"
    )
    if committed_lock != generated:
        raise GateViolation("AUTH-BOOTSTRAP-LOCK-CONTENT", "C is not the generated lock for B")
    return {
        "seed_commit": seed_commit,
        "inventory_commit": inventory_commit,
        "lock_commit": lock_commit,
        "bundle_hash": str(generated["bundle_hash"]),
    }
