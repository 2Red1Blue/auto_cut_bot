"""Synthetic Git authority sources; never real calibrated deployment profiles."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml
from authority.errors import GateViolation
from authority.lock import build_authority_lock
from authority.shadow_context import SHADOW_PROFILE_SCHEMA_PATH, build_locked_shadow_context
from autocut_kernel.contracts.compiler.canonical import sha256_bytes

from tests.authority.test_authority_profile_sources import (
    REPO_ROOT,
    _hash,
    _narrative_mapping,
    _raw,
    _shadow_mapping,
)
from tests.authority.test_lock_and_schema import _commit, _git

NARRATIVE_PATH = "governance/registry-sources/profiles/narrative.json"
SHADOW_PATH = "governance/registry-sources/profiles/shadow.json"
GENERIC_PATH = "governance/registry-sources/profiles/generic-registry-source.json"


@dataclass
class Sources:
    root: Path
    options: dict[str, Any]
    narrative_raw: bytes
    shadow_raw: bytes
    schema_raw: bytes
    lock: dict[str, Any]
    source_commit: str


def _sources(tmp_path: Path, mutation: str = "") -> Sources:
    root = tmp_path / "synthetic-authority"
    root.mkdir()
    _git(root, "init", "-b", "main")
    narrative = _narrative_mapping()
    shadow = _shadow_mapping(narrative)
    if mutation == "contract":
        shadow["profile_contract_sha256"] = _hash("not-the-locked-schema")
    elif mutation == "narrative-reference":
        shadow["stage1_narrative_profile"]["source_sha256"] = _hash("foreign-narrative")
    elif mutation in {"http", "publication"}:
        shadow["capabilities"]["http_media_preflight" if mutation == "http" else "external_publication"] = True
    elif mutation == "local-run-state":
        shadow["profile_state"] = "local_run_v1"
    elif mutation == "unknown-field":
        shadow["unreviewed_override"] = True
    narrative_raw = _raw(narrative)
    shadow_raw = _raw(shadow)
    if mutation == "reformatted-narrative":
        narrative_raw += b"\n"
    if mutation == "duplicate-json-key":
        shadow_raw = shadow_raw[:-1] + b',"profile_version":"1"}'
    schema_raw = (REPO_ROOT / SHADOW_PROFILE_SCHEMA_PATH).read_bytes()
    source_files = [(NARRATIVE_PATH, narrative_raw), (SHADOW_PATH, shadow_raw),
                    (SHADOW_PROFILE_SCHEMA_PATH, schema_raw)]
    if mutation == "generic-profile":
        source_files.append((GENERIC_PATH, b'{"kind":"generic-registry-source"}'))
    for path, raw in source_files:
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    source_commit = _commit(root, "synthetic reviewed sources A")
    entries = [{"repository": "fixture", "class": "registry_source", "path": path}
               for path in (NARRATIVE_PATH, SHADOW_PATH)]
    if mutation == "generic-profile":
        entries.append({"repository": "fixture", "class": "registry_source", "path": GENERIC_PATH})
    entries.append({"repository": "fixture", "class": "schema_source", "path": SHADOW_PROFILE_SCHEMA_PATH})
    for role, path in (("narrative", NARRATIVE_PATH), ("shadow", SHADOW_PATH), ("schema", SHADOW_PROFILE_SCHEMA_PATH)):
        if mutation == f"missing-{role}":
            entries = [item for item in entries if item["path"] != path]
        elif mutation == f"class-{role}":
            next(item for item in entries if item["path"] == path)["class"] = "blocking_fixture"
    inventory = {
        "schema_version": "1.0.0", "authority_id": "synthetic-shadow-authority", "authority_revision": 1,
        "contract_version": "2.1.3", "seed_source_commit": source_commit,
        "repositories": {"fixture": {"source_commit": source_commit}}, "entries": entries,
    }
    (root / "authority-sources.yaml").write_text(yaml.safe_dump(inventory), encoding="utf-8")
    inventory_commit = _commit(root, "inventory-only B")
    lock = build_authority_lock(source_manifest_repository="fixture", source_manifest_commit=inventory_commit,
                                source_manifest_path="authority-sources.yaml", repository_roots={"fixture": root})
    (root / "authority-lock.yaml").write_text(yaml.safe_dump(lock), encoding="utf-8")
    lock_commit = _commit(root, "generated-lock-only C")
    return Sources(root, {
        "repository_roots": {"fixture": root}, "lock_repository": "fixture", "lock_commit": lock_commit,
        "lock_path": "authority-lock.yaml",
        "profile_repository": "fixture", "narrative_path": NARRATIVE_PATH, "shadow_path": SHADOW_PATH,
    }, narrative_raw, shadow_raw, schema_raw, lock, source_commit)


def test_git_verified_shadow_context_derives_every_identity_and_grants_no_runtime(tmp_path: Path) -> None:
    fixture = _sources(tmp_path)
    context = build_locked_shadow_context(**fixture.options)
    assert context.authority_lock_sha256 == fixture.lock["bundle_hash"]
    assert context.profile_source_commit == fixture.source_commit
    assert context.profiles.shadow.source_sha256 == sha256_bytes(fixture.shadow_raw)
    assert context.profiles.narrative.source_sha256 == sha256_bytes(fixture.narrative_raw)
    assert context.profiles.shadow.profile_contract_sha256 == sha256_bytes(fixture.schema_raw)
    assert context.compilation.registry_sha256.startswith("sha256:")
    assert not hasattr(context.compilation, "registry_set")
    assert context.profiles.local_run is None
    assert context.profiles.resolution_state == "grammar_only_unresolved"
    assert not hasattr(context, "bootstrap_request") and not hasattr(context, "snapshot")
    assert context.shadow_path == SHADOW_PATH and context.narrative_path == NARRATIVE_PATH
    assert _git(fixture.root, "status", "--porcelain") == ""


def test_checkout_and_index_bytes_cannot_replace_explicit_committed_profiles(tmp_path: Path) -> None:
    fixture = _sources(tmp_path)
    before = build_locked_shadow_context(**fixture.options)
    for path in (SHADOW_PATH, NARRATIVE_PATH, SHADOW_PROFILE_SCHEMA_PATH, "authority-lock.yaml"):
        (fixture.root / path).write_bytes(b"unreviewed checkout bytes")
    _git(fixture.root, "add", "-A")
    assert build_locked_shadow_context(**fixture.options) == before


@pytest.mark.parametrize("mutation", ["missing-narrative", "missing-shadow", "missing-schema",
                                      "class-narrative", "class-shadow", "class-schema"])
def test_profiles_and_contract_require_exact_lock_classes(tmp_path: Path, mutation: str) -> None:
    fixture = _sources(tmp_path, mutation)
    with pytest.raises(GateViolation):
        build_locked_shadow_context(**fixture.options)


@pytest.mark.parametrize("mutation", ["contract", "narrative-reference", "http", "publication",
                                      "local-run-state", "unknown-field", "reformatted-narrative", "duplicate-json-key"])
def test_immutable_but_invalid_profile_is_not_approved(tmp_path: Path, mutation: str) -> None:
    fixture = _sources(tmp_path, mutation)
    with pytest.raises(GateViolation, match="AUTH-SHADOW-PROFILE"):
        build_locked_shadow_context(**fixture.options)


@pytest.mark.parametrize("field,value", [
    ("shadow_path", "../shadow.json"), ("shadow_path", "profile.json"),
    ("shadow_path", NARRATIVE_PATH), ("narrative_path", "governance/missing.json"),
    ("profile_repository", "unbound"),
])
def test_caller_cannot_substitute_unlocked_profile_locations(tmp_path: Path, field: str, value: str) -> None:
    fixture = _sources(tmp_path)
    with pytest.raises(GateViolation):
        build_locked_shadow_context(**{**fixture.options, field: value})


def test_locked_generic_registry_bytes_cannot_be_substituted_for_a_shadow_profile(tmp_path: Path) -> None:
    fixture = _sources(tmp_path, "generic-profile")
    with pytest.raises(GateViolation, match="AUTH-SHADOW-PROFILE"):
        build_locked_shadow_context(**{**fixture.options, "shadow_path": GENERIC_PATH})
