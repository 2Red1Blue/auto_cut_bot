from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from auto_cut_bot.authority import (
    GovernedRegistryDeployment,
    load_verified_authority_source_snapshot,
)
from tools.authority.lock import build_authority_lock


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def _write(root: Path, path: str, content: str) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _fixture(tmp_path: Path) -> tuple[GovernedRegistryDeployment, bytes]:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.invalid")
    _git(tmp_path, "config", "user.name", "test")
    registry_path = "governance/authority-registry/registry.yaml"
    committed = b"registry: committed\n"
    _write(tmp_path, registry_path, committed.decode())
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "A")
    seed = _git(tmp_path, "rev-parse", "HEAD")
    source = {
        "schema_version": "1.0.0",
        "authority_id": "test-authority",
        "authority_revision": 1,
        "contract_version": "2.1.3",
        "seed_source_commit": seed,
        "repositories": {"authority": {"source_commit": seed}},
        "entries": [{"class": "registry_source", "repository": "authority", "path": registry_path}],
    }
    _write(tmp_path, "governance/authority-sources.yaml", yaml.safe_dump(source, sort_keys=False))
    _git(tmp_path, "add", "governance/authority-sources.yaml")
    _git(tmp_path, "commit", "-qm", "B")
    inventory = _git(tmp_path, "rev-parse", "HEAD")
    lock = build_authority_lock(
        source_manifest_repository="authority",
        source_manifest_commit=inventory,
        source_manifest_path="governance/authority-sources.yaml",
        repository_roots={"authority": tmp_path},
    )
    _write(tmp_path, "governance/authority-lock.yaml", yaml.safe_dump(lock, sort_keys=False))
    _git(tmp_path, "add", "governance/authority-lock.yaml")
    _git(tmp_path, "commit", "-qm", "C")
    return (
        GovernedRegistryDeployment("authority", {"authority": tmp_path}, seed, inventory, _git(tmp_path, "rev-parse", "HEAD")),
        committed,
    )


def test_verified_snapshot_reads_real_abc_locked_registry_bytes(tmp_path: Path) -> None:
    deployment, committed = _fixture(tmp_path)

    snapshot = load_verified_authority_source_snapshot(deployment)

    assert snapshot.registry_sources[0].path == "governance/authority-registry/registry.yaml"
    assert snapshot.registry_sources[0].raw == committed


def test_dirty_checkout_cannot_substitute_locked_registry_bytes(tmp_path: Path) -> None:
    deployment, committed = _fixture(tmp_path)
    _write(tmp_path, "governance/authority-registry/registry.yaml", "registry: dirty-checkout\n")

    snapshot = load_verified_authority_source_snapshot(deployment)

    assert snapshot.registry_sources[0].raw == committed
