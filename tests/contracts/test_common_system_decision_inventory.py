"""B0 must remain a closed decision ledger, never a premature source pack."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

INVENTORY_PATH = (
    Path(__file__).resolve().parents[2]
    / "packages/autocut-kernel/src/autocut_kernel/contracts/source/2_1_3/common"
    / "contracts/system-contracts/common-system-decision-inventory.json"
)

AUTHORITY_COMMIT = "079f0b7c1539a8fb3b7b48f4cd5b0d0cbdc0cb94"
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
DISPOSITIONS = {
    "directly_transcribable",
    "awaiting_owner",
    "authority_change_required",
}
REQUIRED_AC_IDS = [f"AC-B-{number:03d}" for number in range(1, 10)]
FORBIDDEN_TEXT = {"", "...", "TBD", "_example", "placeholder"}

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
        "sha256:95388738e322bb8cd68410dc5e4ca548eff7c8ba496b16d62526d7ca605fcf57"
    ),
    "production-system-contracts-3.4": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-3.5": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-4.1": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-4.4": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-4.8": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-7.1": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-7.2": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-8.4": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-9.1": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-11": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
    "production-system-contracts-12.1": (
        "sha256:c12b24fffd0e534557998f2c125cf517cafe2f4c7a3e0722f62cc8fa68cb7b27"
    ),
}


def _load() -> dict[str, Any]:
    return json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))


def _assert_no_placeholders(value: Any) -> None:
    if isinstance(value, dict):
        for item in value.values():
            _assert_no_placeholders(item)
    elif isinstance(value, list):
        for item in value:
            _assert_no_placeholders(item)
    elif isinstance(value, str):
        assert value not in FORBIDDEN_TEXT


def test_common_system_decision_inventory_has_a_closed_machine_shape() -> None:
    inventory = _load()
    assert set(inventory) == {
        "format",
        "contract_version",
        "inventory_scope",
        "authority",
        "readiness",
        "authority_change_requirements",
        "candidates",
        "prohibitions",
    }
    assert inventory["format"] == "autocut.common-system-decision-inventory/v1"
    assert inventory["contract_version"] == "2.1.3"
    assert inventory["inventory_scope"] == "B0_common_system_source_decisions"
    assert set(inventory["authority"]) == {"commit", "documents"}
    assert inventory["authority"]["commit"] == AUTHORITY_COMMIT
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
