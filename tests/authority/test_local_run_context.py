"""Two synthetic Git chains, not accepted calibration or deployed authority."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pytest
import yaml
from authority.errors import GateViolation
from authority.local_run_context import (
    LOCAL_RUN_PROFILE_SCHEMA_PATH,
    ShadowSourceSelection,
    build_locked_local_run_context,
)
from authority.lock import build_authority_lock
from authority.shadow_context import build_locked_shadow_context
from autocut_kernel.contracts.compiler.canonical import sha256_bytes

from tests.authority.test_authority_profile_sources import REPO_ROOT, _hash, _raw, _run_mapping
from tests.authority.test_lock_and_schema import _commit, _git
from tests.authority.test_shadow_context import NARRATIVE_PATH, Sources, _sources

LOCAL_RUN_PATH = "governance/registry-sources/profiles/local-run.json"


@dataclass
class LocalSources:
    root: Path
    options: dict[str, Any]
    old: Sources
    local_run_raw: bytes
    schema_raw: bytes
    lock: dict[str, Any]
    source_commit: str


def _local_sources(
    tmp_path: Path,
    mutation: str = "",
    *,
    customize_run: Callable[[dict[str, Any], Sources], None] | None = None,
) -> LocalSources:
    """Author a synthetic successor; callback runs before its source commit A."""
    old = _sources(tmp_path)
    root = old.root
    narrative, shadow = json.loads(old.narrative_raw), json.loads(old.shadow_raw)
    run = _run_mapping(narrative, shadow)
    old_context = build_locked_shadow_context(**old.options)
    old_registry_hash = old_context.compilation.registry_sha256
    run["predecessor_shadow_profile"]["registry_set_sha256"] = old_registry_hash
    run["predecessor_shadow_profile"]["authority_lock_sha256"] = old.lock["bundle_hash"]
    if mutation.startswith("predecessor-"):
        field = mutation.removeprefix("predecessor-")
        run["predecessor_shadow_profile"][field] = "2" if field == "profile_version" else _hash("foreign-predecessor")
    elif mutation == "current-registry":
        run["predecessor_shadow_profile"]["registry_set_sha256"] = _hash("current-local-profile-registry")
    elif mutation == "contract":
        run["profile_contract_sha256"] = _hash("other-contract")
    elif mutation == "timing":
        run["timing_policies"]["word_gap_ms"] += 1
    elif mutation == "native":
        run["native_timed_speech"]["service_sha256"] = _hash("other-service")
    elif mutation == "unknown":
        run["trusted_override"] = True
    elif mutation == "publication":
        run["capabilities"]["external_publication"] = True
    elif mutation == "registry-contract":
        run["timed_speech_registry_entry"]["registry_contract_sha256"] = _hash("foreign-contract")
    elif mutation == "whole-schema-as-contract":
        run["timed_speech_registry_entry"]["registry_contract_sha256"] = run["profile_contract_sha256"]
    elif mutation == "registry-set-as-contract":
        run["timed_speech_registry_entry"]["registry_contract_sha256"] = old_registry_hash
    if customize_run is not None:
        customize_run(run, old)
    raw = _raw(run)
    if mutation == "duplicate-key":
        raw = raw[:-1] + b',"profile_version":"1"}'
    schema_raw = (REPO_ROOT / LOCAL_RUN_PROFILE_SCHEMA_PATH).read_bytes()
    if mutation in {"schema-contract-change", "contract-pointer"}:
        schema = json.loads(schema_raw)
        if mutation == "schema-contract-change":
            schema["$defs"]["sha256"]["description"] = "A changed reachable contract definition"
        else:
            schema["properties"]["timed_speech_registry_entry"] = {"$ref": "#/$defs/native_timed_speech"}
        schema_raw = _raw(schema)
        run["profile_contract_sha256"] = sha256_bytes(schema_raw)
        raw = _raw(run)
    if mutation == "schema-raw":
        schema_raw += b"\n"
    if mutation == "narrative-raw":
        (root / NARRATIVE_PATH).write_bytes(old.narrative_raw + b"\n")
    for path, content in ((LOCAL_RUN_PATH, raw), (LOCAL_RUN_PROFILE_SCHEMA_PATH, schema_raw)):
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    source_commit = _commit(root, "successor synthetic sources A")
    entries = [
        {"repository": "fixture", "class": "registry_source", "path": NARRATIVE_PATH},
        {"repository": "fixture", "class": "registry_source", "path": LOCAL_RUN_PATH},
        {"repository": "fixture", "class": "schema_source", "path": LOCAL_RUN_PROFILE_SCHEMA_PATH},
    ]
    for role, path in (("narrative", NARRATIVE_PATH), ("local-run", LOCAL_RUN_PATH), ("schema", LOCAL_RUN_PROFILE_SCHEMA_PATH)):
        if mutation == f"missing-{role}":
            entries = [entry for entry in entries if entry["path"] != path]
        elif mutation == f"class-{role}":
            next(entry for entry in entries if entry["path"] == path)["class"] = "blocking_fixture"
    inventory = {
        "schema_version": "1.0.0", "authority_id": "synthetic-local-run-authority", "authority_revision": 2,
        "contract_version": "2.1.3", "seed_source_commit": source_commit,
        "repositories": {"fixture": {"source_commit": source_commit}}, "entries": entries,
    }
    (root / "authority-sources.yaml").write_text(yaml.safe_dump(inventory))
    inventory_commit = _commit(root, "successor inventory-only B")
    lock = build_authority_lock(source_manifest_repository="fixture", source_manifest_commit=inventory_commit,
                                source_manifest_path="authority-sources.yaml", repository_roots={"fixture": root})
    (root / "authority-lock.yaml").write_text(yaml.safe_dump(lock))
    if mutation == "current-lock-drift":
        (root / "other.txt").write_text("not lock-only")
    lock_commit = _commit(root, "successor generated-lock-only C")
    predecessor = ShadowSourceSelection(**{key: value for key, value in old.options.items() if key != "repository_roots"})
    options = {key: value for key, value in old.options.items() if key != "shadow_path"}
    options.update(lock_commit=lock_commit, local_run_path=LOCAL_RUN_PATH, predecessor=predecessor)
    return LocalSources(root, options, old, raw, schema_raw, lock, source_commit)


def test_current_and_predecessor_are_independently_verified_without_accepting_calibration(tmp_path: Path) -> None:
    fixture = _local_sources(tmp_path)
    context = build_locked_local_run_context(**fixture.options)
    assert context.compilation.registry_sha256 != context.predecessor.compilation.registry_sha256
    assert not hasattr(context.compilation, "registry_set")
    assert context.authority_lock_sha256 == fixture.lock["bundle_hash"] != context.predecessor.authority_lock_sha256
    assert context.profile_source_commit == fixture.source_commit != context.predecessor.profile_source_commit
    assert context.local_run.source_sha256 == sha256_bytes(fixture.local_run_raw)
    assert context.local_run.profile_contract_sha256 == sha256_bytes(fixture.schema_raw)
    assert context.narrative.source_sha256 == sha256_bytes(fixture.old.narrative_raw)
    assert context.local_run.predecessor_shadow_profile.registry_set_sha256 == context.predecessor.compilation.registry_sha256
    assert context.predecessor.authority_lock_sha256 == fixture.old.lock["bundle_hash"]
    assert context.profile_repository == "fixture"
    assert context.narrative_path == NARRATIVE_PATH and context.local_run_path == LOCAL_RUN_PATH
    assert not any(hasattr(context, name) for name in ("anchor", "bootstrap_request", "snapshot", "capability"))
    # The grammar fixture deliberately has placeholder refs; no accepted-record
    # reader or Store is invoked or implied by this source-only success.
    assert context.local_run.calibration.validation_receipt_ref.member_ordinal == 1
    assert _git(fixture.root, "status", "--porcelain") == ""


@pytest.mark.parametrize("mutation", (
    "predecessor-profile_version", "predecessor-source_sha256", "predecessor-registry_set_sha256",
    "predecessor-authority_lock_sha256", "current-registry", "contract", "schema-raw", "narrative-raw",
    "native", "timing", "unknown", "duplicate-key", "publication", "current-lock-drift",
    "missing-narrative", "missing-local-run", "missing-schema", "class-narrative", "class-local-run", "class-schema",
))
def test_locked_current_chain_cannot_certify_foreign_predecessor_or_bad_source(tmp_path: Path, mutation: str) -> None:
    fixture = _local_sources(tmp_path, mutation)
    with pytest.raises(GateViolation):
        build_locked_local_run_context(**fixture.options)


@pytest.mark.parametrize("field,value", (
    ("local_run_path", "../local-run.json"), ("local_run_path", "local-run.json"),
    ("local_run_path", NARRATIVE_PATH), ("local_run_path", "governance/missing.json"),
    ("profile_repository", "unknown"), ("narrative_path", "governance/missing.json"),
))
def test_source_location_substitution_is_rejected(tmp_path: Path, field: str, value: str) -> None:
    fixture = _local_sources(tmp_path)
    with pytest.raises(GateViolation):
        build_locked_local_run_context(**{**fixture.options, field: value})


@pytest.mark.parametrize("mutation", ["registry-contract", "whole-schema-as-contract", "registry-set-as-contract",
                                      "schema-contract-change", "contract-pointer"])
def test_registry_contract_must_derive_from_current_locked_schema(tmp_path: Path, mutation: str) -> None:
    fixture = _local_sources(tmp_path, mutation)
    with pytest.raises(GateViolation, match="AUTH-LOCAL-RUN-CONTRACT"):
        build_locked_local_run_context(**fixture.options)


@pytest.mark.parametrize("mutation", ("current-C", "old-B", "wrong-path", "mapping"))
def test_predecessor_must_replay_its_own_exact_chain(tmp_path: Path, mutation: str) -> None:
    fixture = _local_sources(tmp_path)
    selector = fixture.options["predecessor"]
    if mutation == "current-C":
        selector = replace(selector, lock_commit=fixture.options["lock_commit"])
    elif mutation == "old-B":
        selector = replace(selector, lock_commit=_git(fixture.root, "rev-parse", f"{selector.lock_commit}^"))
    elif mutation == "wrong-path":
        selector = replace(selector, shadow_path=LOCAL_RUN_PATH)
    else:
        selector = fixture.old.options
    with pytest.raises(GateViolation):
        build_locked_local_run_context(**{**fixture.options, "predecessor": selector})


def test_dirty_checkout_and_index_do_not_replace_either_chain(tmp_path: Path) -> None:
    fixture = _local_sources(tmp_path)
    expected = build_locked_local_run_context(**fixture.options)
    for path in (NARRATIVE_PATH, LOCAL_RUN_PATH, fixture.old.options["shadow_path"], LOCAL_RUN_PROFILE_SCHEMA_PATH, "authority-lock.yaml"):
        (fixture.root / path).write_bytes(b"unreviewed source")
    _git(fixture.root, "add", "-A")
    assert build_locked_local_run_context(**fixture.options) == expected
