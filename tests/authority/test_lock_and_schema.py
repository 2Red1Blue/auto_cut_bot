from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from authority.common import canonical_hash
from authority.errors import GateViolation
from authority.lock import (
    build_authority_lock,
    validate_authority_lock,
    verify_authority_lock,
    verify_bootstrap_commit_chain,
)
from authority.receipts import RECEIPT_FIELDS, make_typed_receipt, validate_typed_receipt
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).parents[2]
HASH = "sha256:" + "1" * 64


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(
        root,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.test",
        "commit",
        "-m",
        message,
    )
    return _git(root, "rev-parse", "HEAD")


def _authority_repository(tmp_path: Path) -> tuple[Path, str, str]:
    root = tmp_path / "authority-repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    (root / "contract.md").write_text("reviewed contract\n", encoding="utf-8")
    source_commit = _commit(root, "seed sources")
    source = {
        "schema_version": "1.0.0",
        "authority_id": "fixture-authority",
        "authority_revision": 1,
        "contract_version": "2.1.3",
        "seed_source_commit": source_commit,
        "repositories": {"fixture": {"source_commit": source_commit}},
        "entries": [
            {"class": "production_contract", "repository": "fixture", "path": "contract.md"}
        ],
    }
    (root / "authority-sources.yaml").write_text(yaml.safe_dump(source), encoding="utf-8")
    inventory_commit = _commit(root, "freeze inventory")
    return root, source_commit, inventory_commit


def _build(root: Path, inventory_commit: str) -> dict[str, object]:
    return build_authority_lock(
        source_manifest_repository="fixture",
        source_manifest_commit=inventory_commit,
        source_manifest_path="authority-sources.yaml",
        repository_roots={"fixture": root},
    )


def test_all_governance_json_schemas_are_valid_closed_and_type_specific() -> None:
    schemas = sorted((REPO_ROOT / "governance/schemas").glob("*.schema.json"))
    assert schemas
    for path in schemas:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        assert schema["additionalProperties"] is False
    index = yaml.safe_load((REPO_ROOT / "governance/schema-index.yaml").read_text())
    receipt_targets = [
        path
        for title, path in index["schemas"].items()
        if title.endswith(("Receipt", "Audit", "Manifest", "Set", "Attestation"))
    ]
    assert len(set(receipt_targets)) == len(receipt_targets)
    for receipt_type in RECEIPT_FIELDS:
        assert (
            REPO_ROOT / "governance/schemas" / f"{receipt_type.replace('_', '-')}.schema.json"
        ).is_file()


def test_governance_manifests_validate_against_closed_schemas() -> None:
    pairs = (
        ("authority-sources.yaml", "authority-sources.schema.json"),
        ("blocking-fixtures.manifest.yaml", "blocking-fixtures-manifest.schema.json"),
        ("activation-profiles.yaml", "activation-profiles.schema.json"),
        ("task-authorizations.yaml", "task-authorizations.schema.json"),
        ("model-role-policy.yaml", "model-role-policy.schema.json"),
        ("protected-paths.yaml", "protected-paths.schema.json"),
        ("remote-protection-policy.yaml", "remote-protection-policy.schema.json"),
        (
            "synthetic-sensitive-fixtures.manifest.yaml",
            "synthetic-sensitive-fixture-manifest.schema.json",
        ),
    )
    for document_name, schema_name in pairs:
        document = yaml.safe_load((REPO_ROOT / "governance" / document_name).read_text())
        schema = json.loads((REPO_ROOT / "governance/schemas" / schema_name).read_text())
        Draft202012Validator(schema).validate(document)


def test_git_blob_lock_is_deterministic_and_ignores_dirty_checkout(tmp_path: Path) -> None:
    root, _source_commit, inventory_commit = _authority_repository(tmp_path)
    first = _build(root, inventory_commit)
    (root / "contract.md").write_text("dirty unreviewed bytes\n", encoding="utf-8")
    second = _build(root, inventory_commit)
    assert first == second
    lock_path = root / "authority-lock.yaml"
    lock_path.write_text(yaml.safe_dump(first), encoding="utf-8")
    verified = verify_authority_lock(lock_path, {"fixture": root})
    assert verified["fixture:contract.md"] == first["entries"][0]["sha256"]
    assert verified["inventory"] == first["inventory"]["sha256"]


def test_bootstrap_is_seed_then_inventory_then_lock_without_self_reference(tmp_path: Path) -> None:
    root, seed_commit, inventory_commit = _authority_repository(tmp_path)
    generated = _build(root, inventory_commit)
    assert generated["seed_source_commit"] == seed_commit
    assert generated["inventory"]["manifest_commit"] == inventory_commit
    lock_path = root / "authority-lock.yaml"
    lock_path.write_text(yaml.safe_dump(generated, sort_keys=False), encoding="utf-8")
    lock_commit = _commit(root, "commit generated lock only")
    assert len({seed_commit, inventory_commit, lock_commit}) == 3
    committed = yaml.safe_load(_git(root, "show", f"{lock_commit}:authority-lock.yaml"))
    assert committed == generated
    assert verify_authority_lock(lock_path, {"fixture": root})
    assert _git(root, "diff", "--name-only", lock_commit, "--", "authority-lock.yaml") == ""
    verified = verify_bootstrap_commit_chain(
        repository_root=root,
        seed_commit=seed_commit,
        inventory_commit=inventory_commit,
        lock_commit=lock_commit,
        source_manifest_repository="fixture",
        source_manifest_path="authority-sources.yaml",
        generated_lock_path="authority-lock.yaml",
        repository_roots={"fixture": root},
    )
    assert verified["bundle_hash"] == generated["bundle_hash"]


def test_lock_rejects_dirty_only_source_and_unknown_field(tmp_path: Path) -> None:
    root, _source_commit, inventory_commit = _authority_repository(tmp_path)
    (root / "authority-sources.yaml").write_text("dirty: true\n", encoding="utf-8")
    generated = _build(root, inventory_commit)
    generated["unreviewed_default"] = True
    with pytest.raises(GateViolation, match="AUTH-SCHEMA-EXTRA"):
        validate_authority_lock(generated)


def test_typed_receipt_rejects_cross_type_and_extra_fields() -> None:
    receipt = make_typed_receipt(
        "authority_reference",
        authority_lock_hash=HASH,
        decision="allow",
        reason_codes=[],
        task_id="task",
        staged_tree_hash="1" * 40,
        references_hash=HASH,
        unresolved_ids=[],
    )
    validate_typed_receipt(receipt, expected_type="authority_reference")
    with pytest.raises(GateViolation, match="AUTH-RECEIPT-TYPE"):
        validate_typed_receipt(receipt, expected_type="reuse_admission")
    altered = dict(receipt)
    altered["unexpected"] = True
    with pytest.raises(GateViolation, match="AUTH-SCHEMA-EXTRA"):
        validate_typed_receipt(altered)
    view = dict(receipt)
    produced = view.pop("produced_at")
    expected = view.pop("receipt_id")
    assert produced and canonical_hash(view) == expected


def test_only_locked_activation_receipt_can_use_not_applicable() -> None:
    with pytest.raises(GateViolation, match="AUTH-RECEIPT-DECISION"):
        make_typed_receipt(
            "task_admission",
            authority_lock_hash=HASH,
            decision="not_applicable",
            reason_codes=[],
            task_id="task",
            context_hash=HASH,
            repository_heads_hash=HASH,
            authorization_id=None,
        )
