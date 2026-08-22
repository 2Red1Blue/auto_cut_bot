"""Fail-closed provenance tests for the business-free A1/A2 foundation.

The real package intentionally has no trusted A1/A2 commit pair until the
parent splits and commits it. Positive tests therefore build two temporary Git
checkouts: A1 commits exactly five templates; A2 later commits only its
handoff/review attestation. No test derives a production pin from a handoff.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest
from autocut_kernel.contracts.compiler.canonical import canonical_json_bytes, sha256_bytes
from autocut_kernel.contracts.compiler.errors import RegistryValidationError
from autocut_kernel.contracts.compiler.source_foundation import (
    SourceFoundationTrustPin,
    validate_role_declarations,
    validate_state_machine,
    verify_source_foundation,
)
from jsonschema import Draft202012Validator

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_RELATIVE = Path("packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common")
SOURCE_ROOT = REPOSITORY_ROOT / COMMON_RELATIVE
SCHEMA_META_ROOT = SOURCE_ROOT / "schema-meta"
HANDOFF_ROOT = SOURCE_ROOT / "handoff"
TEMPLATE_PATHS = (
    "handoff/handoff-manifest.schema.json",
    "schema-meta/owner-anchor.schema.json",
    "schema-meta/role-declarations.schema.json",
    "schema-meta/state-machine.schema.json",
    "schema-meta/transition.schema.json",
)
HASH = "sha256:" + "a" * 64


@dataclass(frozen=True, slots=True)
class TwoStepFixture:
    producer_checkout: Path
    source_root: Path
    attestation_checkout: Path
    attestation_root: Path
    authority_checkout: Path
    producer_commit: str
    attestation_commit: str
    pin: SourceFoundationTrustPin


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ("git", "-C", str(root), *args), check=False, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if completed.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {completed.stderr}")
    return completed.stdout.strip()


def _new_repo(root: Path) -> Path:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "foundation-tests@example.invalid")
    _git(root, "config", "user.name", "Foundation Tests")
    return root


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _checkout_at(root: Path, commit: str) -> None:
    """Test-only checkout of a committed temporary fixture history."""

    _git(root, "fetch", "-q", str(root.parent / "producer"), commit)
    _git(root, "reset", "--hard", "-q", commit)


def _source_inventory(source_root: Path) -> tuple[dict[str, str], str, str]:
    inventory = {path: sha256_bytes((source_root / path).read_bytes()) for path in TEMPLATE_PATHS}
    tree_view = [{"path": path, "file_hash": inventory[path]} for path in TEMPLATE_PATHS]
    tree_hash = "sha256:" + hashlib.sha256(canonical_json_bytes(tree_view)).hexdigest()
    inventory_hash = "sha256:" + hashlib.sha256(canonical_json_bytes({"source_paths": tree_view})).hexdigest()
    return inventory, tree_hash, inventory_hash


def _write_a2_attestation(
    attestation_root: Path, source_root: Path, *, producer_commit: str,
    authority_hash: str, extra_source_path: str | None = None,
) -> bytes:
    inventory, tree_hash, inventory_hash = _source_inventory(source_root)
    if extra_source_path is not None:
        inventory[extra_source_path] = sha256_bytes((source_root / extra_source_path).read_bytes())
        tree_view = [{"path": path, "file_hash": digest} for path, digest in sorted(inventory.items())]
        tree_hash = "sha256:" + hashlib.sha256(canonical_json_bytes(tree_view)).hexdigest()
        inventory_hash = "sha256:" + hashlib.sha256(canonical_json_bytes({"source_paths": tree_view})).hexdigest()
    anchors = {"contracts/authority.md#3.1": authority_hash}
    review = {
        "authority_anchors": anchors,
        "format": "autocut.registry-source.review/v1",
        "producer_git_commit": producer_commit,
        "raw_inventory_hash": inventory_hash,
        "reviewer_id": "source_foundation_reviewer",
        "scope": "source_foundation",
        "source_tree_hash": tree_hash,
        "status": "approved",
    }
    review_path = attestation_root / "handoff/source-foundation-review.json"
    review_path.write_bytes(canonical_json_bytes(review))
    handoff = {
        "authority_anchors": anchors,
        "contract_version": "2.1.3",
        "format": "autocut.registry-source.handoff/v1",
        "handoff_version": "1.0.0",
        "producer": {"pack_id": "source_foundation", "producer_git_commit": producer_commit, "source_revision": tree_hash},
        "review": {"producer_git_commit": producer_commit, "review_record_hash": sha256_bytes(review_path.read_bytes()), "reviewer_id": "source_foundation_reviewer"},
        "source_paths": dict(sorted(inventory.items())),
        "source_tree_hash": tree_hash,
    }
    handoff_path = attestation_root / "handoff/source-foundation-handoff.json"
    handoff_path.write_bytes(canonical_json_bytes(handoff))
    return handoff_path.read_bytes()


def _two_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TwoStepFixture:
    import autocut_kernel.contracts.compiler.source_foundation as foundation

    authority = _new_repo(tmp_path / "authority")
    authority_file = authority / "contracts/authority.md"
    authority_file.parent.mkdir(parents=True)
    authority_file.write_bytes(b"# 3.1 Authority\n")
    authority_commit = _commit(authority, "authority")
    monkeypatch.setattr(foundation, "_AUTHORITY_GIT_COMMIT", authority_commit)

    producer = _new_repo(tmp_path / "producer")
    source_root = producer / COMMON_RELATIVE
    for relative in TEMPLATE_PATHS:
        destination = source_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SOURCE_ROOT / relative, destination)
    producer_commit = _commit(producer, "A1 foundation templates")

    attestation = tmp_path / "attestation"
    completed = subprocess.run(("git", "clone", "-q", str(producer), str(attestation)), check=False, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    assert completed.returncode == 0, completed.stderr
    _git(attestation, "config", "user.email", "foundation-tests@example.invalid")
    _git(attestation, "config", "user.name", "Foundation Tests")
    attestation_root = attestation / COMMON_RELATIVE
    handoff_raw = _write_a2_attestation(
        attestation_root, source_root, producer_commit=producer_commit,
        authority_hash=sha256_bytes(authority_file.read_bytes()),
    )
    attestation_commit = _commit(attestation, "A2 attestation")
    return TwoStepFixture(
        producer, source_root, attestation, attestation_root, authority,
        producer_commit, attestation_commit,
        SourceFoundationTrustPin(producer_commit, attestation_commit, sha256_bytes(handoff_raw)),
    )


def _verify(fixture: TwoStepFixture):
    return verify_source_foundation(
        fixture.source_root, attestation_root=fixture.attestation_root,
        authority_root=fixture.authority_checkout, trust_pin=fixture.pin,
        producer_git_checkout=fixture.producer_checkout,
        attestation_git_checkout=fixture.attestation_checkout,
    )


def _validator(directory: Path, name: str) -> Draft202012Validator:
    schema = json.loads((directory / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_foundation_templates_are_closed_2020_12_grammar_only() -> None:
    assert {path.name for path in SCHEMA_META_ROOT.glob("*.schema.json")} == {
        "owner-anchor.schema.json", "role-declarations.schema.json", "state-machine.schema.json", "transition.schema.json",
    }
    for relative in TEMPLATE_PATHS:
        schema = json.loads((SOURCE_ROOT / relative).read_text(encoding="utf-8"))
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert "Authority:" in schema["$comment"]
        Draft202012Validator.check_schema(schema)
        assert "registry_set" not in schema and "entries" not in schema


def test_schema_paths_and_authority_locators_are_ascii_only() -> None:
    anchor = {"owner_pack": "template_pack", "owner_source_path": "schemas/template.schema.json", "owner_source_hash": HASH, "owner_contract_path": "contracts/template.md", "owner_contract_hash": HASH}
    validator = _validator(SCHEMA_META_ROOT, "owner-anchor.schema.json")
    assert validator.is_valid(anchor)
    for unsafe in ("schem\u0430/template.json", "schema/\u202ereversed.json", "schema/\u200bhidden.json", "a//b", "a/./b", "a/../b"):
        assert not validator.is_valid({**anchor, "owner_source_path": unsafe})
    handoff = _validator(HANDOFF_ROOT, "handoff-manifest.schema.json")
    value = {
        "format": "autocut.registry-source.handoff/v1", "contract_version": "2.1.3", "handoff_version": "1.0.0",
        "producer": {"pack_id": "template_pack", "source_revision": HASH, "producer_git_commit": "a" * 40},
        "authority_anchors": {"contracts/%E5%8E%9F%E7%90%86.md#3.1": HASH},
        "source_paths": {TEMPLATE_PATHS[0]: HASH}, "source_tree_hash": HASH,
        "review": {"reviewer_id": "reviewer", "review_record_hash": HASH, "producer_git_commit": "a" * 40},
    }
    assert handoff.is_valid(value)
    value["authority_anchors"] = {"contracts/\u539f\u7406.md#3.1": HASH}
    assert not handoff.is_valid(value)


def test_template_semantics_remain_business_free() -> None:
    validate_role_declarations({"input": [], "policy": [], "lifecycle_slots": []})
    with pytest.raises(RegistryValidationError, match="min_refs exceeds"):
        validate_role_declarations({"input": [{"role": "r", "artifact_types": ["a"], "scope_kinds": ["s"], "min_refs": 2, "max_refs": 1}], "policy": [], "lifecycle_slots": []})
    validate_state_machine({"states": ["state:a", "state:b"], "transitions": [{"transition_id": "transition:a", "from_state": "state:a", "to_state": "state:b"}]})


def test_a1_snapshot_matches_exact_a1_blobs_and_a2_attestation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    result = _verify(fixture)
    assert tuple(path for path, _ in result.source_paths) == TEMPLATE_PATHS
    assert {item.path for item in result.source_snapshot} == {*TEMPLATE_PATHS, "handoff/source-foundation-handoff.json", "handoff/source-foundation-review.json"}


def test_a1_and_a2_checkouts_must_be_exact_and_distinct(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    with pytest.raises(RegistryValidationError, match="distinct"):
        verify_source_foundation(fixture.source_root, attestation_root=fixture.source_root, authority_root=fixture.authority_checkout, trust_pin=fixture.pin, producer_git_checkout=fixture.producer_checkout, attestation_git_checkout=fixture.producer_checkout)
    _git(fixture.producer_checkout, "commit", "--allow-empty", "-q", "-m", "wrong A1 head")
    with pytest.raises(RegistryValidationError, match="producer git checkout HEAD"):
        _verify(fixture)


def test_missing_a1_git_blob_fails_even_when_untracked_bytes_exist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    missing = TEMPLATE_PATHS[-1]
    _git(fixture.producer_checkout, "rm", str(COMMON_RELATIVE / missing))
    missing_commit = _commit(fixture.producer_checkout, "remove A1 template")
    _checkout_at(fixture.attestation_checkout, missing_commit)
    replacement = fixture.source_root / missing
    replacement.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE_ROOT / missing, replacement)
    raw = _write_a2_attestation(fixture.attestation_root, fixture.source_root, producer_commit=missing_commit, authority_hash=sha256_bytes((fixture.authority_checkout / "contracts/authority.md").read_bytes()))
    a3 = _commit(fixture.attestation_checkout, "A2 negative reattestation")
    pin = SourceFoundationTrustPin(missing_commit, a3, sha256_bytes(raw))
    with pytest.raises(RegistryValidationError, match="exactly the five approved"):
        verify_source_foundation(fixture.source_root, attestation_root=fixture.attestation_root, authority_root=fixture.authority_checkout, trust_pin=pin, producer_git_checkout=fixture.producer_checkout, attestation_git_checkout=fixture.attestation_checkout)


def test_a1_and_a2_must_not_share_a_commit_even_in_distinct_clones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    _git(fixture.attestation_checkout, "reset", "--hard", "-q", fixture.producer_commit)
    same_commit = SourceFoundationTrustPin(
        fixture.producer_commit,
        fixture.producer_commit,
        fixture.pin.handoff_raw_hash,
    )
    with pytest.raises(RegistryValidationError, match="commits must be distinct"):
        verify_source_foundation(
            fixture.source_root,
            attestation_root=fixture.attestation_root,
            authority_root=fixture.authority_checkout,
            trust_pin=same_commit,
            producer_git_checkout=fixture.producer_checkout,
            attestation_git_checkout=fixture.attestation_checkout,
        )


def test_a2_must_descend_from_a1_not_merely_contain_its_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    unrelated = _new_repo(tmp_path / "unrelated")
    (unrelated / "note.txt").write_text("unrelated\n", encoding="utf-8")
    unrelated_commit = _commit(unrelated, "unrelated A2")
    _git(unrelated, "fetch", "-q", str(fixture.producer_checkout), fixture.producer_commit)
    pin = SourceFoundationTrustPin(fixture.producer_commit, unrelated_commit, fixture.pin.handoff_raw_hash)
    with pytest.raises(RegistryValidationError, match="must be an ancestor"):
        verify_source_foundation(
            fixture.source_root,
            attestation_root=unrelated,
            authority_root=fixture.authority_checkout,
            trust_pin=pin,
            producer_git_checkout=fixture.producer_checkout,
            attestation_git_checkout=unrelated,
        )


def test_a1_a2_git_worktrees_with_one_common_directory_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    linked = tmp_path / "linked-attestation"
    _git(fixture.producer_checkout, "worktree", "add", "-q", "-b", "foundation-a2", str(linked), fixture.producer_commit)
    _git(linked, "config", "user.email", "foundation-tests@example.invalid")
    _git(linked, "config", "user.name", "Foundation Tests")
    linked_root = linked / COMMON_RELATIVE
    raw = _write_a2_attestation(
        linked_root,
        fixture.source_root,
        producer_commit=fixture.producer_commit,
        authority_hash=sha256_bytes((fixture.authority_checkout / "contracts/authority.md").read_bytes()),
    )
    linked_commit = _commit(linked, "linked A2 attestation")
    pin = SourceFoundationTrustPin(fixture.producer_commit, linked_commit, sha256_bytes(raw))
    with pytest.raises(RegistryValidationError, match="distinct Git common directories"):
        verify_source_foundation(
            fixture.source_root,
            attestation_root=linked_root,
            authority_root=fixture.authority_checkout,
            trust_pin=pin,
            producer_git_checkout=fixture.producer_checkout,
            attestation_git_checkout=linked,
        )


def test_dirty_authority_worktree_does_not_replace_pinned_authority_blob(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    authority_file = fixture.authority_checkout / "contracts/authority.md"
    authority_file.write_text("# 3.1 Dirty local rewrite\n", encoding="utf-8")
    assert _verify(fixture).authority_anchors


def test_committed_unlisted_a1_template_file_is_rejected_by_tree_inventory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    evil = fixture.source_root / "schema-meta/evil-business-entry.schema.json"
    evil.write_text('{"not":"approved"}\n', encoding="utf-8")
    evil_commit = _commit(fixture.producer_checkout, "unlisted foundation source")
    _checkout_at(fixture.attestation_checkout, evil_commit)
    raw = _write_a2_attestation(
        fixture.attestation_root,
        fixture.source_root,
        producer_commit=evil_commit,
        authority_hash=sha256_bytes((fixture.authority_checkout / "contracts/authority.md").read_bytes()),
    )
    a2 = _commit(fixture.attestation_checkout, "A2 attest evil A1")
    pin = SourceFoundationTrustPin(evil_commit, a2, sha256_bytes(raw))
    with pytest.raises(RegistryValidationError, match="exactly the five approved"):
        verify_source_foundation(
            fixture.source_root,
            attestation_root=fixture.attestation_root,
            authority_root=fixture.authority_checkout,
            trust_pin=pin,
            producer_git_checkout=fixture.producer_checkout,
            attestation_git_checkout=fixture.attestation_checkout,
        )


def test_evil_extra_source_path_and_resigning_a2_are_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    extra = "schema-meta/evil-business-entry.json"
    (fixture.source_root / extra).write_bytes(b'{"artifact":"not-allowed"}')
    raw = _write_a2_attestation(fixture.attestation_root, fixture.source_root, producer_commit=fixture.producer_commit, authority_hash=sha256_bytes((fixture.authority_checkout / "contracts/authority.md").read_bytes()), extra_source_path=extra)
    a3 = _commit(fixture.attestation_checkout, "A2 evil re-sign")
    pin = SourceFoundationTrustPin(fixture.producer_commit, a3, sha256_bytes(raw))
    with pytest.raises(RegistryValidationError, match="exactly the five A1 foundation templates"):
        verify_source_foundation(fixture.source_root, attestation_root=fixture.attestation_root, authority_root=fixture.authority_checkout, trust_pin=pin, producer_git_checkout=fixture.producer_checkout, attestation_git_checkout=fixture.attestation_checkout)


def test_re_signing_cannot_authorize_worktree_template_mutation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    (fixture.source_root / TEMPLATE_PATHS[-1]).write_bytes(b"{}")
    raw = _write_a2_attestation(fixture.attestation_root, fixture.source_root, producer_commit=fixture.producer_commit, authority_hash=sha256_bytes((fixture.authority_checkout / "contracts/authority.md").read_bytes()))
    a3 = _commit(fixture.attestation_checkout, "A2 mutated template re-sign")
    pin = SourceFoundationTrustPin(fixture.producer_commit, a3, sha256_bytes(raw))
    with pytest.raises(RegistryValidationError, match="producer Git blob does not match"):
        verify_source_foundation(fixture.source_root, attestation_root=fixture.attestation_root, authority_root=fixture.authority_checkout, trust_pin=pin, producer_git_checkout=fixture.producer_checkout, attestation_git_checkout=fixture.attestation_checkout)


def test_re_signing_uncommitted_a2_handoff_cannot_bypass_pinned_attestation_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    handoff_path = fixture.attestation_root / "handoff/source-foundation-handoff.json"
    changed = json.loads(handoff_path.read_text(encoding="utf-8"))
    changed["handoff_version"] = "1.0.1"
    handoff_path.write_bytes(canonical_json_bytes(changed))
    re_signed = SourceFoundationTrustPin(fixture.producer_commit, fixture.attestation_commit, sha256_bytes(handoff_path.read_bytes()))
    with pytest.raises(RegistryValidationError, match="A2 attestation Git blob does not match"):
        verify_source_foundation(fixture.source_root, attestation_root=fixture.attestation_root, authority_root=fixture.authority_checkout, trust_pin=re_signed, producer_git_checkout=fixture.producer_checkout, attestation_git_checkout=fixture.attestation_checkout)


def test_authority_head_heading_and_percent_encoded_path_are_pinned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fixture = _two_step(tmp_path, monkeypatch)
    assert _verify(fixture).authority_anchors
    _git(fixture.authority_checkout, "commit", "--allow-empty", "-q", "-m", "wrong authority head")
    with pytest.raises(RegistryValidationError, match="authority git checkout HEAD"):
        _verify(fixture)


def test_real_authority_integration_requires_explicit_external_environment_pin() -> None:
    """CI enables this only after parent creates the real A1 and A2 commits."""
    required = ("AUTOCUT_FOUNDATION_A1_CHECKOUT", "AUTOCUT_FOUNDATION_A1_SOURCE_ROOT", "AUTOCUT_FOUNDATION_A2_CHECKOUT", "AUTOCUT_FOUNDATION_A2_ROOT", "AUTOCUT_FOUNDATION_AUTHORITY_ROOT", "AUTOCUT_FOUNDATION_A1_COMMIT", "AUTOCUT_FOUNDATION_A2_COMMIT", "AUTOCUT_FOUNDATION_A2_HANDOFF_SHA256")
    if any(not os.environ.get(name) for name in required):
        pytest.skip("real A1/A2 integration requires explicit external CI pins")
    pin = SourceFoundationTrustPin(os.environ["AUTOCUT_FOUNDATION_A1_COMMIT"], os.environ["AUTOCUT_FOUNDATION_A2_COMMIT"], os.environ["AUTOCUT_FOUNDATION_A2_HANDOFF_SHA256"])
    result = verify_source_foundation(Path(os.environ["AUTOCUT_FOUNDATION_A1_SOURCE_ROOT"]), attestation_root=Path(os.environ["AUTOCUT_FOUNDATION_A2_ROOT"]), authority_root=Path(os.environ["AUTOCUT_FOUNDATION_AUTHORITY_ROOT"]), trust_pin=pin, producer_git_checkout=Path(os.environ["AUTOCUT_FOUNDATION_A1_CHECKOUT"]), attestation_git_checkout=Path(os.environ["AUTOCUT_FOUNDATION_A2_CHECKOUT"]))
    assert tuple(path for path, _ in result.source_paths) == TEMPLATE_PATHS
