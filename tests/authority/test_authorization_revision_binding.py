from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from authority.common import canonical_hash, sha256_bytes
from authority.errors import GateViolation
from authority.lock import build_authority_lock, verify_authority_lock_data

AUTHORIZATIONS_PATH = "governance/task-authorizations.yaml"
MANIFEST_PATH = "governance/authority-sources.yaml"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _git_bytes(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


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


def _authority_repository(
    tmp_path: Path,
    *,
    authorization_revision: object = 9,
    inventory_revision: int = 9,
    authorization_class: str = "architecture_gate",
    duplicate_authorization: bool = False,
    include_authorization: bool = True,
) -> tuple[Path, str, str]:
    root = tmp_path / "authority-repository"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "governance").mkdir()
    (root / "governance/qc-source.yaml").write_text("checks: []\n", encoding="utf-8")
    if include_authorization:
        (root / AUTHORIZATIONS_PATH).write_text(
            yaml.safe_dump({"authority_revision": authorization_revision}), encoding="utf-8"
        )
    source_commit = _commit(root, "reviewed sources")
    entries: list[dict[str, str]] = [
        {
            "class": "production_contract",
            "repository": "fixture",
            "path": "governance/qc-source.yaml",
        }
    ]
    if include_authorization:
        authorization_entry = {
            "class": authorization_class,
            "repository": "fixture",
            "path": AUTHORIZATIONS_PATH,
        }
        entries.append(authorization_entry)
        if duplicate_authorization:
            entries.append(dict(authorization_entry))
    source = {
        "schema_version": "1.0.0",
        "authority_id": "fixture-authority",
        "authority_revision": inventory_revision,
        "contract_version": "2.1.3",
        "seed_source_commit": source_commit,
        "repositories": {"fixture": {"source_commit": source_commit}},
        "entries": entries,
    }
    (root / MANIFEST_PATH).write_text(yaml.safe_dump(source), encoding="utf-8")
    return root, source_commit, _commit(root, "authority inventory")


def _build(root: Path, inventory_commit: str) -> dict[str, Any]:
    return build_authority_lock(
        source_manifest_repository="fixture",
        source_manifest_commit=inventory_commit,
        source_manifest_path=MANIFEST_PATH,
        repository_roots={"fixture": root},
    )


def _hash_valid_lock(root: Path, source_commit: str, inventory_commit: str) -> dict[str, Any]:
    source_raw = _git_bytes(root, "show", f"{inventory_commit}:{MANIFEST_PATH}")
    source = yaml.safe_load(source_raw)
    entries = [
        {
            "class": entry["class"],
            "repository": entry["repository"],
            "path": entry["path"],
            "sha256": sha256_bytes(_git_bytes(root, "show", f"{source_commit}:{entry['path']}")),
        }
        for entry in source["entries"]
    ]
    entries.sort(key=lambda item: (item["repository"], item["path"], item["class"]))
    lock: dict[str, Any] = {
        "schema_version": source["schema_version"],
        "authority_id": source["authority_id"],
        "authority_revision": source["authority_revision"],
        "contract_version": source["contract_version"],
        "seed_source_commit": source_commit,
        "inventory": {
            "repository": "fixture",
            "manifest_commit": inventory_commit,
            "path": MANIFEST_PATH,
            "sha256": sha256_bytes(source_raw),
        },
        "repositories": {"fixture": {"source_commit": source_commit}},
        "entries": entries,
    }
    lock["bundle_hash"] = canonical_hash(lock)
    return lock


def test_authorization_revision_uses_immutable_corrected_revision_nine(tmp_path: Path) -> None:
    root, _source_commit, inventory_commit = _authority_repository(tmp_path)
    lock = _build(root, inventory_commit)
    assert verify_authority_lock_data(lock, {"fixture": root})[f"fixture:{AUTHORIZATIONS_PATH}"]

    (root / AUTHORIZATIONS_PATH).write_text("authority_revision: 7\n", encoding="utf-8")
    _git(root, "add", AUTHORIZATIONS_PATH)
    assert verify_authority_lock_data(lock, {"fixture": root})[f"fixture:{AUTHORIZATIONS_PATH}"]


def test_authorization_revision_rejects_old_source_for_builder_and_replay(tmp_path: Path) -> None:
    root, source_commit, inventory_commit = _authority_repository(
        tmp_path, authorization_revision=7, inventory_revision=8
    )
    with pytest.raises(GateViolation, match="AUTH-LOCK-AUTHORIZATION-REVISION"):
        _build(root, inventory_commit)

    lock = _hash_valid_lock(root, source_commit, inventory_commit)
    with pytest.raises(GateViolation, match="AUTH-LOCK-AUTHORIZATION-REVISION"):
        verify_authority_lock_data(lock, {"fixture": root})

    tampered = dict(lock)
    tampered["entries"] = [dict(entry) for entry in lock["entries"]]
    next(entry for entry in tampered["entries"] if entry["path"] == AUTHORIZATIONS_PATH)[
        "sha256"
    ] = "sha256:" + "0" * 64
    tampered["bundle_hash"] = canonical_hash(
        {key: value for key, value in tampered.items() if key != "bundle_hash"}
    )
    with pytest.raises(GateViolation, match="AUTH-LOCK-FILE-HASH"):
        verify_authority_lock_data(tampered, {"fixture": root})


@pytest.mark.parametrize(
    ("kwargs", "error"),
    (
        ({"authorization_class": "schema_source"}, "AUTH-LOCK-AUTHORIZATION-SOURCE"),
        ({"authorization_revision": True}, "AUTH-LOCK-AUTHORIZATION-REVISION"),
    ),
)
def test_authorization_source_rejects_invalid_class_or_revision_in_builder_and_replay(
    tmp_path: Path, kwargs: dict[str, object], error: str
) -> None:
    root, source_commit, inventory_commit = _authority_repository(tmp_path, **kwargs)
    with pytest.raises(GateViolation, match=error):
        _build(root, inventory_commit)
    with pytest.raises(GateViolation, match=error):
        verify_authority_lock_data(
            _hash_valid_lock(root, source_commit, inventory_commit), {"fixture": root}
        )


def test_authorization_source_duplicate_retains_existing_generic_duplicate_error(
    tmp_path: Path,
) -> None:
    root, _source_commit, inventory_commit = _authority_repository(
        tmp_path, duplicate_authorization=True
    )
    with pytest.raises(GateViolation, match="AUTH-SOURCE-DUPLICATE"):
        _build(root, inventory_commit)


def test_lock_without_task_authorization_source_remains_supported(tmp_path: Path) -> None:
    root, _source_commit, inventory_commit = _authority_repository(
        tmp_path, include_authorization=False
    )
    assert verify_authority_lock_data(_build(root, inventory_commit), {"fixture": root})
