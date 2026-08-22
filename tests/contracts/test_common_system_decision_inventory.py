"""B0 must remain a closed decision ledger, never a premature source pack."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
COMMON_ROOT = (
    REPOSITORY_ROOT
    / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common"
)
SYSTEM_CONTRACTS_ROOT = COMMON_ROOT / "contracts/system-contracts"
INVENTORY_PATH = SYSTEM_CONTRACTS_ROOT / "common-system-decision-inventory.json"
PROVENANCE_PATH = SYSTEM_CONTRACTS_ROOT / "common-system-provenance.json"
INVENTORY_GIT_PATH = INVENTORY_PATH.relative_to(REPOSITORY_ROOT).as_posix()

FOUNDATION_PRODUCER_COMMIT = "1fd66f6598b950b19349a44113569c04e840a84f"
FOUNDATION_HANDOFF_COMMIT = "168b71d9fa9d20e9c2dc1061f80073ac0a0078de"
FOUNDATION_HANDOFF_RAW_SHA256 = (
    "sha256:c0be3d51e1ff8d8bd6fa1a5a8e907832e6bb58816fb4b6137776bd9abca57c2d"
)
INITIAL_INVENTORY_COMMIT = "0eff1bc1c2a52e6a601fe50ca85e344a29dca1a3"
INITIAL_AUTHORITY_COMMIT = "079f0b7c1539a8fb3b7b48f4cd5b0d0cbdc0cb94"
INITIAL_INVENTORY_RAW_SHA256 = (
    "sha256:c3ef8e479c713be3dfb1e21b51bb1e0f4aa29c51b210366d53af353168655901"
)
REVIEWED_DELTA_AUTHORITY_COMMIT = "0e3ad0766409df67fbefeeb3d04fab4ea843319b"
REVIEWED_DELTA_SOURCE_MAP_RAW_SHA256 = (
    "sha256:53d62bbe23569ea5f6ea1c3a7fa6d41c70e21b6372aad52f43790b9ab866b12a"
)
REVIEWED_DELTA_ENTRY_COUNT = 44
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
DISPOSITIONS = {
    "directly_transcribable",
    "awaiting_owner",
    "authority_change_required",
}
REQUIRED_AC_IDS = [f"AC-B-{number:03d}" for number in range(1, 10)]
FORBIDDEN_TEXT = {"", "...", "TBD", "_example", "placeholder"}
FORBIDDEN_IDENTITY_KEYS = {
    "artifact_id",
    "artifact_type",
    "command_id",
    "command_name",
    "evaluator_component_id",
    "handler_component_id",
    "idempotency_component_id",
    "registry_id",
    "rule_id",
    "schema_id",
    "state_id",
    "strategy_id",
    "trace_id",
    "transition_id",
}

EXPECTED_CANDIDATES = {
    "schemas/primitives/artifact-ref.schema.json",
    "schemas/primitives/artifact-set-ref.schema.json",
    "schemas/primitives/degradation.schema.json",
    "schemas/primitives/diagnostic.schema.json",
    "schemas/primitives/domain-ref.schema.json",
    "schemas/primitives/immutable-blob-ref.schema.json",
    "schemas/primitives/scope.schema.json",
    "schemas/primitives/source-span-ref.schema.json",
    "schemas/envelope/artifact-envelope.schema.json",
    "schemas/bootstrap/external-job-request.schema.json",
    "schemas/bootstrap/job-start-slot.schema.json",
    "schemas/run/run-manifest.schema.json",
    "schemas/admission/admission-result.schema.json",
    "schemas/admission/rule-result.schema.json",
    "schemas/receipt/command-request-shell.schema.json",
    "schemas/receipt/command-result-shell.schema.json",
    "schemas/receipt/command-receipt-registry.schema.json",
    "schemas/receipt/command-receipt.schema.json",
    "schemas/recovery/recovery-attempt.schema.json",
    "schemas/recovery/recovery-ledger.schema.json",
    "schemas/generation-audit/generated-draft.schema.json",
    "schemas/generation-audit/generation-attempt-slot.schema.json",
    "schemas/generation-audit/generation-invocation.schema.json",
    "schemas/generation-audit/generation-policy.schema.json",
    "schemas/generation-audit/model-input-manifest.schema.json",
    "schemas/generation-audit/parse-normalization-record.schema.json",
    "schemas/migration/migration-assessment.schema.json",
    "schemas/migration/migration-policy.schema.json",
    "schemas/source-usage-ledger.schema.json",
    "schemas/stage_05/qc-report.schema.json",
    "schemas/stage_05/render-attempt-result.schema.json",
    "schemas/stage_05/rendered-asset.schema.json",
    "schemas/publication/batch-publication-plan.schema.json",
    "schemas/publication/independent-ledger.schema.json",
    "schemas/publication/platform-publication-capability.schema.json",
    "schemas/publication/platform-spec-policy.schema.json",
    "schemas/publication/publication-enablement.schema.json",
    "schemas/publication/publication-ledger.schema.json",
    "schemas/publication/publication-policy.schema.json",
    "schemas/publication/publication-target.schema.json",
    "schemas/publication/qc-policy.schema.json",
    "schemas/publication/sensitive-content-policy.schema.json",
    "schemas/publication/story-portfolio-release.schema.json",
    "schemas/publication/transaction-artifacts.schema.json",
}

EXPECTED_AUTHORITY_ANCHORS = [
    "implementation-contract-toolchain-3.1",
    "production-system-contracts-3.4",
    "production-system-contracts-3.5",
    "production-system-contracts-4.1",
    "production-system-contracts-4.4",
    "production-system-contracts-4.8",
    "production-system-contracts-7.1",
    "production-system-contracts-7.2",
    "production-system-contracts-8.4",
    "production-system-contracts-9.1",
    "production-system-contracts-11",
    "production-system-contracts-12.1",
]

EXPECTED_AUTHORITY_DOCUMENT_HASHES = {
    "implementation-contract-toolchain-3.1": (
        "sha256:8ab12ebe823a62b06af32433bd857e2b1d67e68faee278d5c963891fb36a60c2"
    ),
    "production-system-contracts-3.4": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-3.5": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-4.1": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-4.4": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-4.8": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-7.1": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-7.2": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-8.4": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-9.1": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-11": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
    "production-system-contracts-12.1": (
        "sha256:9418faf746c2a710219b060a9c5ec020bedad1927e6516975eb2938943d54707"
    ),
}


def _sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _git_bytes(repository: Path, *arguments: str) -> bytes:
    completed = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise AssertionError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout


def _git_text(repository: Path, *arguments: str) -> str:
    return _git_bytes(repository, *arguments).decode("ascii").strip()


def _git_blob(repository: Path, commit: str, path: str) -> bytes:
    return _git_bytes(repository, "show", f"{commit}:{path}")


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_bytes())
    assert isinstance(value, dict)
    return value


def _load() -> dict[str, Any]:
    return _load_json(INVENTORY_PATH)


def _load_provenance() -> dict[str, Any]:
    return _load_json(PROVENANCE_PATH)


def _capture_external_file(root: Path, relative: str) -> bytes:
    relative_path = Path(relative)
    assert not relative_path.is_absolute()
    assert relative_path.parts and all(
        part not in {"", ".", ".."} for part in relative_path.parts
    )
    assert root.is_dir() and not root.is_symlink()
    cursor = root
    for part in relative_path.parts[:-1]:
        cursor /= part
        assert not cursor.is_symlink()
    candidate = root / relative_path
    descriptor = os.open(
        candidate,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        assert stat.S_ISREG(metadata.st_mode), candidate
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65536):
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _capture_pinned_external_file(root: Path, relative: str, expected_hash: str) -> bytes:
    assert SHA256.fullmatch(expected_hash)
    raw = _capture_external_file(root, relative)
    assert _sha256(raw) == expected_hash
    return raw


def _external_roots() -> tuple[Path, Path]:
    authority_value = os.environ.get("AUTOCUT_B0_AUTHORITY_REPOSITORY")
    task_value = os.environ.get("AUTOCUT_B0_TRELLIS_TASKS_ROOT")
    if authority_value is None and task_value is None:
        pytest.skip("real B0 authority/task integration requires explicit external roots")
    assert authority_value is not None, (
        "AUTOCUT_B0_AUTHORITY_REPOSITORY is required when any B0 external pin is supplied"
    )
    assert task_value is not None, (
        "AUTOCUT_B0_TRELLIS_TASKS_ROOT is required when any B0 external pin is supplied"
    )
    authority_root = Path(authority_value)
    task_root = Path(task_value)
    assert authority_root.is_dir() and not authority_root.is_symlink()
    assert task_root.is_dir() and not task_root.is_symlink()
    return authority_root, task_root


def _assert_no_business_identity_declarations(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            assert key not in FORBIDDEN_IDENTITY_KEYS
            if key.startswith("declares_"):
                assert item is False
            _assert_no_business_identity_declarations(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_business_identity_declarations(item)


def _assert_no_placeholders(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_placeholders(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_placeholders(item)
    elif isinstance(value, str):
        assert value not in FORBIDDEN_TEXT


def test_common_system_provenance_has_a_closed_non_business_shape() -> None:
    provenance = _load_provenance()
    assert set(provenance) == {
        "format",
        "contract_version",
        "inventory_scope",
        "foundation_input",
        "authority_input",
        "design_input",
        "inventory_output",
        "readiness",
    }
    assert provenance["format"] == "autocut.common-system-provenance/v1"
    assert provenance["contract_version"] == "2.1.3"
    assert provenance["inventory_scope"] == "B0_common_system_source_decisions"
    foundation = provenance["foundation_input"]
    assert set(foundation) == {
        "producer_commit",
        "handoff_commit",
        "handoff_git_path",
        "handoff_raw_sha256",
    }
    assert foundation == {
        "producer_commit": FOUNDATION_PRODUCER_COMMIT,
        "handoff_commit": FOUNDATION_HANDOFF_COMMIT,
        "handoff_git_path": (
            "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/"
            "common/handoff/source-foundation-handoff.json"
        ),
        "handoff_raw_sha256": FOUNDATION_HANDOFF_RAW_SHA256,
    }
    authority = provenance["authority_input"]
    assert set(authority) == {
        "initial_inventory_snapshot",
        "reviewed_delta_snapshot",
        "refresh_relation",
    }
    initial = authority["initial_inventory_snapshot"]
    assert set(initial) == {
        "inventory_commit",
        "inventory_git_path",
        "inventory_raw_sha256",
        "authority_commit",
    }
    assert initial["inventory_commit"] == INITIAL_INVENTORY_COMMIT
    assert initial["inventory_git_path"] == INVENTORY_GIT_PATH
    assert initial["authority_commit"] == INITIAL_AUTHORITY_COMMIT
    assert initial["inventory_raw_sha256"] == INITIAL_INVENTORY_RAW_SHA256
    reviewed_delta = authority["reviewed_delta_snapshot"]
    assert set(reviewed_delta) == {
        "task_source_map_path",
        "task_source_map_raw_sha256",
        "authority_commit",
        "documents",
        "entry_reattestation",
    }
    assert reviewed_delta["authority_commit"] == REVIEWED_DELTA_AUTHORITY_COMMIT
    assert reviewed_delta["task_source_map_raw_sha256"] == REVIEWED_DELTA_SOURCE_MAP_RAW_SHA256
    assert reviewed_delta["documents"] == sorted(
        reviewed_delta["documents"], key=lambda item: item["path"].encode("utf-8")
    )
    assert all(
        set(document) == {"path", "raw_utf8_sha256", "task_source_map_binding"}
        and document["path"].endswith(".md")
        and SHA256.fullmatch(document["raw_utf8_sha256"])
        and document["task_source_map_binding"]
        in {"listed_authority", "design_mechanics_not_listed_in_authority_table"}
        for document in reviewed_delta["documents"]
    )
    assert {
        document["task_source_map_binding"] for document in reviewed_delta["documents"]
    } == {"listed_authority", "design_mechanics_not_listed_in_authority_table"}
    assert reviewed_delta["entry_reattestation"] == {
        "scope": "all_authority_backed_B0_entries",
        "entry_count": REVIEWED_DELTA_ENTRY_COUNT,
        "inventory_raw_sha256": _sha256(INVENTORY_PATH.read_bytes()),
    }
    assert authority["refresh_relation"] == {
        "initial_inventory_created_before_current_task_map": True,
        "required_git_relation": (
            "initial_authority_is_ancestor_of_reviewed_delta_authority"
        ),
        "required_blob_relation": "reviewed_delta_authority_documents_match_pinned_hashes",
    }
    assert set(provenance["design_input"]) == {
        "task_design_path",
        "task_design_raw_sha256",
    }
    assert SHA256.fullmatch(provenance["design_input"]["task_design_raw_sha256"])
    assert provenance["inventory_output"]["path"] == (
        "contracts/system-contracts/common-system-decision-inventory.json"
    )
    assert _sha256(INVENTORY_PATH.read_bytes()) == provenance["inventory_output"][
        "raw_sha256"
    ]
    assert provenance["readiness"] == {
        "common_pack_ready": False,
        "business_source_authorized": False,
        "registry_contribution_authorized": False,
    }
    _assert_no_placeholders(provenance)


def test_foundation_and_initial_b0_pins_resolve_to_actual_git_blobs() -> None:
    provenance = _load_provenance()
    foundation = provenance["foundation_input"]
    assert _git_text(REPOSITORY_ROOT, "rev-parse", foundation["producer_commit"]) == (
        FOUNDATION_PRODUCER_COMMIT
    )
    assert _git_text(REPOSITORY_ROOT, "rev-parse", foundation["handoff_commit"]) == (
        FOUNDATION_HANDOFF_COMMIT
    )
    _git_bytes(
        REPOSITORY_ROOT,
        "merge-base",
        "--is-ancestor",
        foundation["producer_commit"],
        foundation["handoff_commit"],
    )
    handoff_raw = _git_blob(
        REPOSITORY_ROOT,
        foundation["handoff_commit"],
        foundation["handoff_git_path"],
    )
    assert _sha256(handoff_raw) == foundation["handoff_raw_sha256"]
    handoff = json.loads(handoff_raw)
    assert handoff["producer"]["producer_git_commit"] == foundation["producer_commit"]
    assert handoff["review"]["producer_git_commit"] == foundation["producer_commit"]
    common_git_root = foundation["handoff_git_path"].rsplit("/handoff/", 1)[0]
    source_tree = []
    for relative, expected_hash in handoff["source_paths"].items():
        source_raw = _git_blob(
            REPOSITORY_ROOT,
            foundation["producer_commit"],
            f"{common_git_root}/{relative}",
        )
        assert _sha256(source_raw) == expected_hash
        source_tree.append({"path": relative, "file_hash": expected_hash})
    canonical_tree = json.dumps(
        source_tree,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert _sha256(canonical_tree) == handoff["source_tree_hash"]
    assert handoff["producer"]["source_revision"] == handoff["source_tree_hash"]

    initial = provenance["authority_input"]["initial_inventory_snapshot"]
    assert _git_text(REPOSITORY_ROOT, "rev-parse", initial["inventory_commit"]) == (
        INITIAL_INVENTORY_COMMIT
    )
    assert _git_text(REPOSITORY_ROOT, "rev-parse", f"{INITIAL_INVENTORY_COMMIT}^") == (
        FOUNDATION_HANDOFF_COMMIT
    )
    initial_raw = _git_blob(
        REPOSITORY_ROOT,
        initial["inventory_commit"],
        initial["inventory_git_path"],
    )
    assert _sha256(initial_raw) == initial["inventory_raw_sha256"]
    assert json.loads(initial_raw)["authority"]["commit"] == initial["authority_commit"]


def test_external_authority_map_design_and_git_blobs_are_one_fresh_snapshot() -> None:
    authority_root, task_root = _external_roots()
    provenance = _load_provenance()
    authority = provenance["authority_input"]
    reviewed_delta = authority["reviewed_delta_snapshot"]
    source_map_raw = _capture_pinned_external_file(
        task_root,
        reviewed_delta["task_source_map_path"],
        reviewed_delta["task_source_map_raw_sha256"],
    )
    source_map_text = source_map_raw.decode("utf-8")
    commit_match = re.search(r"commit\s+`([0-9a-f]{7,64})`", source_map_text)
    assert commit_match is not None
    assert _git_text(authority_root, "rev-parse", commit_match.group(1)) == reviewed_delta[
        "authority_commit"
    ]
    _git_bytes(
        authority_root,
        "merge-base",
        "--is-ancestor",
        authority["initial_inventory_snapshot"]["authority_commit"],
        reviewed_delta["authority_commit"],
    )
    inventory = _load()
    assert inventory["authority"]["commit"] == reviewed_delta["authority_commit"]
    accepted_hashes = {
        document["path"]: document["raw_utf8_sha256"]
        for document in reviewed_delta["documents"]
    }
    inventory_hashes = {
        document["path"]: document["raw_utf8_sha256"]
        for document in inventory["authority"]["documents"]
    }
    assert inventory_hashes == accepted_hashes
    for document in reviewed_delta["documents"]:
        path = document["path"]
        expected_hash = document["raw_utf8_sha256"]
        if document["task_source_map_binding"] == "listed_authority":
            assert f"`{Path(path).name}`" in source_map_text
            assert expected_hash.removeprefix("sha256:") in source_map_text
        else:
            assert Path(path).name not in source_map_text
        accepted_raw = _git_blob(
            authority_root,
            reviewed_delta["authority_commit"],
            path,
        )
        assert _sha256(accepted_raw) == expected_hash

    assert len(inventory["candidates"]) == reviewed_delta["entry_reattestation"][
        "entry_count"
    ]

    design = provenance["design_input"]
    design_raw = _capture_pinned_external_file(
        task_root,
        design["task_design_path"],
        design["task_design_raw_sha256"],
    )
    design_text = design_raw.decode("utf-8")
    assert all(requirement_id in design_text for requirement_id in REQUIRED_AC_IDS)
    assert "B0：锁定输入并建立 blocker ledger" in design_text


def test_external_pin_parameterization_is_fail_closed_when_partially_supplied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AUTOCUT_B0_AUTHORITY_REPOSITORY", str(tmp_path))
    monkeypatch.delenv("AUTOCUT_B0_TRELLIS_TASKS_ROOT", raising=False)
    with pytest.raises(AssertionError, match="TRELLIS_TASKS_ROOT"):
        _external_roots()


def test_external_file_capture_rejects_hash_drift_and_path_escape(tmp_path: Path) -> None:
    pinned = tmp_path / "pinned.md"
    pinned.write_bytes(b"frozen\n")
    with pytest.raises(AssertionError):
        _capture_pinned_external_file(tmp_path, "pinned.md", "sha256:" + "0" * 64)
    with pytest.raises(AssertionError):
        _capture_external_file(tmp_path, "../escape.md")


def test_common_system_decision_inventory_has_a_closed_machine_shape() -> None:
    inventory = _load()
    assert set(inventory) == {
        "format",
        "contract_version",
        "inventory_scope",
        "provenance_ref",
        "authority",
        "readiness",
        "authority_change_requirements",
        "candidates",
        "prohibitions",
    }
    assert inventory["format"] == "autocut.common-system-decision-inventory/v2"
    assert inventory["contract_version"] == "2.1.3"
    assert inventory["inventory_scope"] == "B0_common_system_source_decisions"
    assert inventory["provenance_ref"] == (
        "contracts/system-contracts/common-system-provenance.json"
    )
    assert set(inventory["authority"]) == {"commit", "documents"}
    assert inventory["authority"]["commit"] == REVIEWED_DELTA_AUTHORITY_COMMIT
    assert inventory["authority"]["documents"]
    _assert_no_placeholders(inventory)


def test_common_system_decision_inventory_is_ordered_and_pinned_to_raw_markdown() -> None:
    inventory = _load()
    documents = inventory["authority"]["documents"]
    assert all(
        set(document)
        == {
            "anchor_id",
            "path",
            "section",
            "raw_utf8_sha256",
            "jcs_hash_applicability",
        }
        for document in documents
    )
    assert [document["anchor_id"] for document in documents] == EXPECTED_AUTHORITY_ANCHORS
    assert all(document["path"].endswith(".md") for document in documents)
    assert all(SHA256.fullmatch(document["raw_utf8_sha256"]) for document in documents)
    assert {
        document["anchor_id"]: document["raw_utf8_sha256"] for document in documents
    } == EXPECTED_AUTHORITY_DOCUMENT_HASHES
    assert all(
        document["jcs_hash_applicability"] == "not_applicable_non_json_markdown"
        for document in documents
    )


def test_common_system_decision_inventory_covers_every_b_design_candidate() -> None:
    inventory = _load()
    candidates = inventory["candidates"]
    assert all(
        set(candidate)
        == {
            "candidate_id",
            "proposed_source",
            "disposition",
            "owner",
            "authority_anchor_ids",
            "authority_change_ids",
        }
        for candidate in candidates
    )
    assert [candidate["candidate_id"] for candidate in candidates] == [
        f"B0-{number:03d}" for number in range(1, len(candidates) + 1)
    ]
    assert {candidate["proposed_source"] for candidate in candidates} == EXPECTED_CANDIDATES
    assert all(candidate["disposition"] in DISPOSITIONS for candidate in candidates)
    assert all(candidate["authority_anchor_ids"] for candidate in candidates)
    anchor_ids = {
        document["anchor_id"] for document in inventory["authority"]["documents"]
    }
    assert {
        anchor_id
        for candidate in candidates
        for anchor_id in candidate["authority_anchor_ids"]
    } <= anchor_ids
    assert all(
        candidate["authority_anchor_ids"] == sorted(candidate["authority_anchor_ids"])
        and candidate["authority_change_ids"] == sorted(candidate["authority_change_ids"])
        for candidate in candidates
    )
    assert all(
        "registries/" not in candidate["proposed_source"]
        and not candidate["proposed_source"].endswith("registry_set.yaml")
        for candidate in candidates
    )


def test_common_system_decision_inventory_preserves_all_open_authority_changes() -> None:
    inventory = _load()
    requirements = inventory["authority_change_requirements"]
    assert all(set(requirement) == {"id", "subject"} for requirement in requirements)
    assert [requirement["id"] for requirement in requirements] == REQUIRED_AC_IDS
    requirement_ids = {requirement["id"] for requirement in requirements}
    candidate_ids = {
        requirement_id
        for candidate in inventory["candidates"]
        for requirement_id in candidate["authority_change_ids"]
    }
    assert candidate_ids <= requirement_ids
    assert candidate_ids == requirement_ids
    assert all(
        candidate["authority_change_ids"]
        for candidate in inventory["candidates"]
        if candidate["disposition"] == "authority_change_required"
    )


def test_common_system_decision_inventory_cannot_claim_pack_or_business_readiness() -> None:
    inventory = _load()
    assert inventory["readiness"] == {
        "common_pack_ready": False,
        "registry_entries_declared": False,
        "business_schema_declared": False,
        "reason": (
            "B0 records source decisions and blockers only; later owner contributions and "
            "closed authority decisions are required before any RegistrySet can be ready."
        ),
    }
    assert inventory["prohibitions"] == {
        "declares_registry_entry": False,
        "declares_artifact_identity": False,
        "declares_business_default": False,
        "declares_command_identity": False,
        "declares_rule_identity": False,
        "declares_strategy_identity": False,
        "declares_trace_identity": False,
    }


def test_b0_source_subtree_contains_only_decisions_and_provenance() -> None:
    files = {
        path.relative_to(SYSTEM_CONTRACTS_ROOT).as_posix()
        for path in SYSTEM_CONTRACTS_ROOT.rglob("*")
        if path.is_file()
    }
    assert files == {
        "common-system-decision-inventory.json",
        "common-system-provenance.json",
    }
    assert not any(
        "registries" in path.parts
        or path.name == "registry_set.yaml"
        or "generated" in path.parts
        for path in SYSTEM_CONTRACTS_ROOT.rglob("*")
    )
    for path in sorted(SYSTEM_CONTRACTS_ROOT.glob("*.json")):
        _assert_no_business_identity_declarations(_load_json(path))
