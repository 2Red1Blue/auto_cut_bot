"""Synthetic Git-chain coverage; fixtures never establish deployed authority."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
import yaml
from autocut_kernel.contracts.compiler.errors import RegistryValidationError
from autocut_kernel.contracts.compiler.registry import RegistrySet
from autocut_kernel.contracts.compiler.registry_source import compile_registry_source

from tests.authority.test_lock_and_schema import _commit, _git
from tests.contracts.test_registry_source_manifest import (
    _mutate_document_and_resign,
    _write_closed_source,
)
from tools.authority.common import canonical_hash, load_mapping_bytes
from tools.authority.errors import GateViolation
from tools.authority.lock import build_authority_lock
from tools.authority.locked_registry import compile_locked_registry, read_locked_blob

REGISTRY_ROOT = "governance/registry-sources/closed"
LOCK_PATH = "authority-lock.yaml"


def _locked_repository(tmp_path: Path, drift: str = "") -> tuple[Path, str]:
    root = tmp_path / "authority"
    root.mkdir()
    _git(root, "init", "-b", "main")
    source = _write_closed_source(root / REGISTRY_ROOT)
    if drift in {"extra", "unlocked-extra"}:
        (source / "extra.json").write_text("{}")
    elif drift == "symlink":
        target = source / "common/entry.json"
        target.unlink()
        target.symlink_to("schema.json")
    elif drift == "executable":
        (source / "common/entry.json").chmod(0o755)
    elif drift == "not-ready":
        _mutate_document_and_resign(source, "strategies", lambda entries: entries.clear())
    elif drift == "raw-source-drift":
        (source / "common/entry.json").write_text('{"substituted":true}')
    source_commit = _commit(root, "A: reviewed synthetic source")
    paths = sorted(path.relative_to(root).as_posix() for path in source.rglob("*") if path.is_file())
    if drift == "missing":
        paths.remove(f"{REGISTRY_ROOT}/common/entry.json")
    elif drift == "unlocked-extra":
        paths.remove(f"{REGISTRY_ROOT}/extra.json")
    entries = [{"class": "registry_source", "repository": "fixture", "path": path} for path in paths]
    if drift == "class":
        entries[0]["class"] = "schema_source"
    inventory = {
        "schema_version": "1.0.0", "authority_id": "synthetic-only", "authority_revision": 1,
        "contract_version": "2.1.3", "seed_source_commit": source_commit,
        "repositories": {"fixture": {"source_commit": source_commit}}, "entries": entries,
    }
    (root / "authority-sources.yaml").write_text(yaml.safe_dump(inventory))
    if drift == "inventory-diff":
        (root / "unrelated.txt").write_text("not an inventory change")
    inventory_commit = _commit(root, "B: inventory only")
    lock = build_authority_lock(
        source_manifest_repository="fixture", source_manifest_commit=inventory_commit,
        source_manifest_path="authority-sources.yaml", repository_roots={"fixture": root},
    )
    if drift == "lock-hash":
        lock["entries"][0]["sha256"] = "sha256:" + "1" * 64
        lock["bundle_hash"] = canonical_hash({key: value for key, value in lock.items() if key != "bundle_hash"})
    (root / LOCK_PATH).write_text(yaml.safe_dump(lock))
    if drift == "lock-diff":
        (root / "unrelated.txt").write_text("not a generated lock")
    lock_commit = _commit(root, "C: generated lock only")
    return root, lock_commit


def _compile(root: Path, commit: str):
    return compile_locked_registry(
        repository_roots={"fixture": root}, lock_repository="fixture", lock_commit=commit,
        lock_path=LOCK_PATH, registry_repository="fixture", registry_root=REGISTRY_ROOT,
    )


@pytest.mark.parametrize("drift", ("", "executable"))
def test_real_git_chain_compiles_ready_and_leaves_no_private_paths(tmp_path: Path, drift: str) -> None:
    root, commit = _locked_repository(tmp_path, drift)
    before = _git(root, "status", "--porcelain")
    compiled = _compile(root, commit)
    assert compiled.registry_set == compile_registry_source(root / REGISTRY_ROOT)
    assert compiled.registry_set.ready
    assert compiled.lock_raw == (root / LOCK_PATH).read_bytes()
    assert (compiled.lock_repository, compiled.lock_commit, compiled.lock_path) == ("fixture", commit, LOCK_PATH)
    assert (compiled.registry_repository, compiled.registry_root) == ("fixture", REGISTRY_ROOT)
    assert _git(root, "status", "--porcelain") == before == ""
    assert "autocut-locked-registry-" not in repr(compiled)
    with pytest.raises(FrozenInstanceError):
        compiled.lock_commit = "0" * 40


def test_dirty_checkout_index_and_missing_checkout_cannot_change_locked_output(tmp_path: Path) -> None:
    root, commit = _locked_repository(tmp_path)
    original = _compile(root, commit)
    (root / REGISTRY_ROOT / "common/entry.json").write_text("dirty source")
    (root / LOCK_PATH).write_text("dirty: lock")
    _git(root, "add", "-A")
    (root / REGISTRY_ROOT).rename(root / "detached-checkout")
    (root / "auto_cut_bot.config.json").write_text('{"untrusted":true}')
    before = _git(root, "status", "--porcelain")
    assert _compile(root, commit) == original
    assert _git(root, "status", "--porcelain") == before


@pytest.mark.parametrize("drift, error", (
    ("inventory-diff", "AUTH-BOOTSTRAP-INVENTORY-DIFF"),
    ("lock-diff", "AUTH-BOOTSTRAP-LOCK-DIFF"),
    ("lock-hash", "AUTH-BOOTSTRAP-LOCK-CONTENT"),
    ("class", "AUTH-REGISTRY-ENTRY"),
    ("missing", "AUTH-REGISTRY-COVERAGE"),
    ("unlocked-extra", "AUTH-REGISTRY-COVERAGE"),
    ("extra", "AUTH-REGISTRY-COVERAGE"),
    ("symlink", "AUTH-REGISTRY-BLOB"),
))
def test_real_chain_rejects_drift_class_coverage_and_symlink(
    tmp_path: Path, drift: str, error: str,
) -> None:
    root, commit = _locked_repository(tmp_path, drift)
    with pytest.raises(GateViolation, match=error):
        _compile(root, commit)


@pytest.mark.parametrize("drift", ("not-ready", "raw-source-drift"))
def test_lock_coverage_never_bypasses_real_compiler(tmp_path: Path, drift: str) -> None:
    root, commit = _locked_repository(tmp_path, drift)
    with pytest.raises(RegistryValidationError):
        _compile(root, commit)


@pytest.mark.parametrize("field, value", (
    ("lock_commit", "HEAD"), ("lock_commit", "0" * 40),
    ("lock_repository", "unbound"), ("registry_repository", "unbound"),
    ("lock_path", "../authority-lock.yaml"), ("lock_path", "/authority-lock.yaml"),
    ("registry_root", "../closed"), ("registry_root", "governance//closed"),
    ("registry_root", "."), ("registry_root", "missing"),
))
def test_exact_selector_rejects_invalid_repository_revision_and_paths(tmp_path: Path, field: str, value: str) -> None:
    root, commit = _locked_repository(tmp_path)
    arguments = {
        "lock_repository": "fixture", "lock_commit": commit, "lock_path": LOCK_PATH,
        "registry_repository": "fixture", "registry_root": REGISTRY_ROOT, field: value,
    }
    with pytest.raises(GateViolation):
        compile_locked_registry(repository_roots={"fixture": root}, **arguments)


def test_full_oid_must_name_c_not_inventory_or_later_commit(tmp_path: Path) -> None:
    root, commit = _locked_repository(tmp_path)
    with pytest.raises(GateViolation):
        _compile(root, _git(root, "rev-parse", f"{commit}^"))
    (root / "later.txt").write_text("later")
    later = _commit(root, "not C")
    with pytest.raises(GateViolation, match="AUTH-BOOTSTRAP-LOCK-PARENT"):
        _compile(root, later)


@pytest.mark.parametrize("drift", ("class", "path", "hash", "repository"))
def test_blob_helper_checks_exact_entry_but_does_not_self_certify_authority(tmp_path: Path, drift: str) -> None:
    root, commit = _locked_repository(tmp_path)
    compiled = _compile(root, commit)
    lock = load_mapping_bytes(compiled.lock_raw, where="verified C")
    path = f"{REGISTRY_ROOT}/common/entry.json"
    expected = (root / path).read_bytes()
    arguments = {"repository": "fixture", "path": path, "expected_class": "registry_source"}
    assert read_locked_blob(lock=lock, repository_roots={"fixture": root}, **arguments) == expected
    if drift == "hash":
        next(entry for entry in lock["entries"] if entry["path"] == path)["sha256"] = "sha256:" + "1" * 64
        lock["bundle_hash"] = canonical_hash({key: value for key, value in lock.items() if key != "bundle_hash"})
    else:
        key = {"class": "expected_class", "path": "path", "repository": "repository"}[drift]
        arguments[key] = {"class": "schema_source", "path": "../entry.json", "repository": "other"}[drift]
    with pytest.raises(GateViolation):
        read_locked_blob(lock=lock, repository_roots={"fixture": root}, **arguments)


def test_compile_api_does_not_accept_self_asserted_mapping_or_registry(tmp_path: Path) -> None:
    root, commit = _locked_repository(tmp_path)
    compiled = _compile(root, commit)
    assert isinstance(compiled.registry_set, RegistrySet)
    for injected in ({"lock": json.loads(json.dumps(yaml.safe_load(compiled.lock_raw)))},
                     {"registry_set": compiled.registry_set}):
        with pytest.raises(TypeError):
            compile_locked_registry(
                repository_roots={"fixture": root}, lock_repository="fixture", lock_commit=commit,
                lock_path=LOCK_PATH, registry_repository="fixture", registry_root=REGISTRY_ROOT,
                **injected,
            )
