"""Immutable A/B/C-governed registry-source byte loader."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, cast

from tools.authority.common import git_bytes, load_mapping_bytes, require_commit, sha256_bytes
from tools.authority.errors import GateViolation
from tools.authority.lock import verify_authority_lock_data, verify_bootstrap_commit_chain

_LOCK_PATH = "governance/authority-lock.yaml"
_SOURCE_MANIFEST_PATH = "governance/authority-sources.yaml"
_REGISTRY_PREFIX = "governance/authority-registry/"


class GovernedRegistrySnapshotError(ValueError):
    """The deployment-provided A/B/C authority chain cannot yield registry bytes."""


@dataclass(frozen=True, slots=True)
class GovernedRegistryDeployment:
    """Explicit deployment injection; not populated from HTTP, env, or checkout paths."""

    authority_repository: str
    repository_roots: Mapping[str, Path]
    seed_commit: str
    inventory_commit: str
    lock_commit: str

    def __post_init__(self) -> None:
        if type(self.authority_repository) is not str or not self.authority_repository.strip():  # noqa: E721
            raise GovernedRegistrySnapshotError("authority repository name is required")
        if type(self.repository_roots) is not dict:  # noqa: E721
            raise GovernedRegistrySnapshotError("repository roots must be an explicit mapping")
        raw_roots = cast(dict[str, Path], self.repository_roots)
        if not raw_roots:
            raise GovernedRegistrySnapshotError("repository roots must be an explicit mapping")
        if self.authority_repository not in raw_roots:
            raise GovernedRegistrySnapshotError("authority repository root is missing")
        for value, label in (
            (self.seed_commit, "seed commit"),
            (self.inventory_commit, "inventory commit"),
            (self.lock_commit, "lock commit"),
        ):
            try:
                require_commit(value, where=label)
            except GateViolation as error:
                raise GovernedRegistrySnapshotError(f"{label} is invalid") from error
        if any(type(name) is not str for name in raw_roots):  # noqa: E721
            raise GovernedRegistrySnapshotError("repository roots must contain string names")
        roots = {name: root.resolve(strict=True) for name, root in raw_roots.items()}
        if any(not path.is_dir() for path in roots.values()):
            raise GovernedRegistrySnapshotError("repository root is not a directory")
        object.__setattr__(self, "repository_roots", roots)


@dataclass(frozen=True, slots=True)
class VerifiedRegistrySourceBytes:
    repository: str
    path: str
    sha256: str
    raw: bytes


@dataclass(frozen=True, slots=True)
class VerifiedAuthoritySourceSnapshot:
    """Only immutable registry-source bytes selected by the governed lock."""

    lock_commit: str
    bundle_hash: str
    registry_sources: tuple[VerifiedRegistrySourceBytes, ...]


def load_verified_authority_source_snapshot(
    deployment: GovernedRegistryDeployment,
) -> VerifiedAuthoritySourceSnapshot:
    """Verify A→B→C and return exactly the lock-listed registry source blobs."""

    if type(deployment) is not GovernedRegistryDeployment:  # noqa: E721
        raise GovernedRegistrySnapshotError("governed deployment locator must be exact")
    root = deployment.repository_roots[deployment.authority_repository]
    try:
        lock = load_mapping_bytes(
            git_bytes(root, deployment.lock_commit, _LOCK_PATH),
            where=f"{deployment.lock_commit}:{_LOCK_PATH}",
            suffix=".yaml",
        )
        verify_authority_lock_data(lock, deployment.repository_roots)
        chain = verify_bootstrap_commit_chain(
            repository_root=root,
            seed_commit=deployment.seed_commit,
            inventory_commit=deployment.inventory_commit,
            lock_commit=deployment.lock_commit,
            source_manifest_repository=deployment.authority_repository,
            source_manifest_path=_SOURCE_MANIFEST_PATH,
            generated_lock_path=_LOCK_PATH,
            repository_roots=deployment.repository_roots,
        )
    except (GateViolation, OSError, ValueError) as error:
        raise GovernedRegistrySnapshotError("governed authority lock chain is invalid") from error
    selected: list[VerifiedRegistrySourceBytes] = []
    seen: set[tuple[str, str]] = set()
    for entry in lock["entries"]:
        if entry["class"] != "registry_source":
            continue
        repository, path = entry["repository"], entry["path"]
        if repository != deployment.authority_repository or not path.startswith(_REGISTRY_PREFIX):
            raise GovernedRegistrySnapshotError("registry source is outside governed authority registry")
        identity = (repository, path)
        if identity in seen:
            raise GovernedRegistrySnapshotError("duplicate registry source is forbidden")
        seen.add(identity)
        raw = git_bytes(
            deployment.repository_roots[repository],
            lock["repositories"][repository]["source_commit"],
            path,
        )
        if sha256_bytes(raw) != entry["sha256"]:
            raise GovernedRegistrySnapshotError("registry source digest does not match authority lock")
        selected.append(VerifiedRegistrySourceBytes(repository, path, entry["sha256"], raw))
    if not selected:
        raise GovernedRegistrySnapshotError("authority lock has no governed registry sources")
    selected.sort(key=lambda item: (item.repository, item.path))
    return VerifiedAuthoritySourceSnapshot(deployment.lock_commit, chain["bundle_hash"], tuple(selected))
