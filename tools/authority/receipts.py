# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false
"""Closed, type-specific Phase -1 receipt contracts.

The schema generator and runtime validator share one field registry so a new
receipt cannot silently exist only in documentation or only in Python.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .common import (
    canonical_hash,
    require_closed,
    require_commit,
    require_list,
    require_non_empty_string,
    require_sha256,
    utc_now,
)
from .errors import GateViolation

FieldKind = str

RECEIPT_FIELDS: dict[str, dict[str, FieldKind]] = {
    "consumer_lock_readiness": {
        "task_id": "string",
        "authority_governance_commit": "git_oid",
        "authority_bundle_hash": "sha256",
        "consumer_repository_commit": "git_oid",
        "consumer_commit_tree_oid": "git_oid",
        "consumer_index_tree_oid": "git_oid",
        "consumer_lock_path": "reserved_consumer_lock_path",
        "state": "consumer_lock_readiness_state",
        "reason": "consumer_lock_readiness_reason",
        "profile_policy_hash": "sha256",
    },
    "kernel_build": {
        "task_id": "string",
        "authority_governance_commit": "git_oid",
        "authority_lock_document_hash": "sha256",
        "authority_bundle_hash": "sha256",
        "build_evidence_policy_hash": "sha256",
        "kernel_source_commit": "git_oid",
        "kernel_source_subtree_hash": "sha256",
        "distribution_name": "string",
        "distribution_version": "string",
        "wheel_filename": "string",
        "wheel_tag": "string",
        "wheel_size_bytes": "positive_integer",
        "wheel_sha256": "sha256",
        "build_recipe_hash": "sha256",
        "environment_lock_hash": "sha256",
        "provenance_receipt_hash": "sha256",
    },
    "consumer_lock": {
        "task_id": "string",
        "authority_governance_commit": "git_oid",
        "authority_bundle_hash": "sha256",
        "kernel_build_receipt_hash": "sha256",
        "consumer_lock_blob_hash": "sha256",
        "consumer_lock_document_hash": "sha256",
        "consumer_repository_commit": "git_oid",
        "consumer_commit_tree_oid": "git_oid",
        "eligibility_profile": "consumer_lock_profile",
        "profile_policy_hash": "sha256",
    },
    "source_candidate": {
        "task_id": "string",
        "predecessor_commit": "git_oid",
        "candidate_tree_hash": "git_oid",
        "changed_paths_hash": "sha256",
        "synthetic_fixture_manifest_hash": "sha256",
    },
    "task_admission": {
        "task_id": "string",
        "context_hash": "sha256",
        "repository_heads_hash": "sha256",
        "authorization_id": "nullable_string",
    },
    "authority_reference": {
        "task_id": "string",
        "staged_tree_hash": "git_oid",
        "references_hash": "sha256",
        "unresolved_ids": "string_array",
    },
    "reuse_admission": {
        "task_id": "string",
        "staged_tree_hash": "git_oid",
        "ledger_hash": "sha256",
        "observed_imports_hash": "sha256",
        "violations": "string_array",
    },
    "change_scope": {
        "task_id": "string",
        "repository": "string",
        "base_commit": "git_oid",
        "staged_tree_hash": "git_oid",
        "index_tree_hash": "git_oid",
        "changed_paths_hash": "sha256",
        "worktree_policy_hash": "sha256",
    },
    "validation_receipt_set": {
        "task_id": "string",
        "staged_tree_hash": "git_oid",
        "environment_hash": "sha256",
        "command_results_hash": "sha256",
        "runner_attestation_hash": "sha256",
        "failed_command_ids": "string_array",
    },
    "candidate_tree_audit": {
        "task_id": "string",
        "staged_tree_hash": "git_oid",
        "index_tree_hash": "git_oid",
        "path_set_hash": "sha256",
        "findings": "string_array",
    },
    "independent_check": {
        "task_id": "string",
        "implementer_run_identity": "string",
        "checker_run_identity": "string",
        "implementation_context_hash": "sha256",
        "check_context_hash": "sha256",
        "candidate_tree_hash": "git_oid",
        "checker_input_manifest_hash": "sha256",
        "protected_oracle_hashes_hash": "sha256",
        "checker_run_attestation_hash": "sha256",
        "checker_command_results_hash": "sha256",
    },
    "commit_tree": {
        "task_id": "string",
        "candidate_commit": "git_oid",
        "committed_tree_hash": "git_oid",
        "approved_staged_tree_hash": "git_oid",
    },
    "history_publication_audit": {
        "task_id": "string",
        "remote_canonical_url": "string",
        "target_ref": "string",
        "expected_remote_oid": "git_oid",
        "candidate_commit": "git_oid",
        "candidate_tree_hash": "git_oid",
        "range_algorithm": "string",
        "commit_set_hash": "sha256",
        "blob_set_hash": "sha256",
        "findings": "string_array",
        "policy_hash": "sha256",
        "remote_protection_attestation_hash": "sha256",
        "fetched_at": "date_time",
        "expires_at": "date_time",
    },
    "upstream_parity": {
        "task_id": "string",
        "upstream_base": "git_oid",
        "upstream_head": "git_oid",
        "local_base": "git_oid",
        "local_candidate": "git_oid",
        "capability_inventory_hash": "sha256",
        "mapping_set_hash": "sha256",
        "unmapped_capabilities": "string_array",
        "protected_zero_diff": "boolean",
    },
    "baseline_failure": {
        "task_id": "string",
        "baseline_commit": "git_oid",
        "candidate_commit": "git_oid",
        "command_hash": "sha256",
        "environment_hash": "sha256",
        "failure_signature_hash": "sha256",
        "baseline_signature_hash": "sha256",
        "related_inputs_hash": "sha256",
        "changed_scope_hash": "sha256",
        "classification_owner": "string",
        "classification": "string",
    },
    "runtime_conformance": {
        "task_id": "string",
        "predicate_id": "string",
        "profile": "string",
        "minimum_phase": "phase",
        "current_phase": "phase",
        "status": "predicate_status",
        "reason": "string",
        "profile_policy_hash": "sha256",
        "profile_signature": "sha256",
    },
    "remote_protection": {
        "task_id": "nullable_string",
        "remote_canonical_url": "string",
        "target_ref": "string",
        "expected_remote_oid": "git_oid",
        "candidate_commit": "git_oid",
        "policy_hash": "sha256",
        "collector_evidence_hash": "sha256",
        "collector_id": "string",
        "protection_enabled": "boolean",
        "required_checks_hash": "sha256",
        "fetched_at": "date_time",
        "expires_at": "date_time",
    },
    "change_verification": {
        "task_id": "string",
        "base_commits_hash": "sha256",
        "repository_tree_oids_hash": "sha256",
        "control_plane_context_hash": "sha256",
        "receipt_closure_hash": "sha256",
    },
    "push_verification": {
        "task_id": "string",
        "candidate_commit": "git_oid",
        "candidate_tree_hash": "git_oid",
        "expected_remote_oid": "git_oid",
        "target_ref": "string",
        "receipt_closure_hash": "sha256",
    },
}

TITLES = {
    "consumer_lock_readiness": "ConsumerLockReadinessReceipt",
    "kernel_build": "KernelBuildReceipt",
    "consumer_lock": "ConsumerLockReceipt",
    "source_candidate": "SourceCandidateReceipt",
    "task_admission": "TaskAdmissionReceipt",
    "authority_reference": "AuthorityReferenceReceipt",
    "reuse_admission": "ReuseAdmissionReceipt",
    "change_scope": "ChangeScopeReceipt",
    "validation_receipt_set": "ValidationReceiptSet",
    "candidate_tree_audit": "CandidateTreeAudit",
    "independent_check": "IndependentCheckReceipt",
    "commit_tree": "CommitTreeReceipt",
    "history_publication_audit": "HistoryPublicationAudit",
    "upstream_parity": "UpstreamParityManifest",
    "baseline_failure": "BaselineFailureManifest",
    "runtime_conformance": "RuntimeConformanceReceipt",
    "remote_protection": "RemoteProtectionAttestation",
    "change_verification": "ChangeVerificationReceipt",
    "push_verification": "PushVerificationReceipt",
}

NOT_APPLICABLE_RECEIPT_TYPES = frozenset({"consumer_lock_readiness", "runtime_conformance"})

PHASES = [
    "phase_minus_1",
    "phase_0",
    "phase_1",
    "phase_2",
    "phase_3",
    "phase_4",
    "phase_5",
    "phase_6",
]


def _schema_for_kind(kind: FieldKind) -> dict[str, Any]:
    if kind == "string":
        return {"type": "string", "minLength": 1}
    if kind == "nullable_string":
        return {"type": ["string", "null"], "minLength": 1}
    if kind == "sha256":
        return {"$ref": "#/$defs/sha256"}
    if kind == "git_oid":
        return {"$ref": "#/$defs/git_oid"}
    if kind == "string_array":
        return {"type": "array", "items": {"type": "string", "minLength": 1}}
    if kind == "boolean":
        return {"type": "boolean"}
    if kind == "date_time":
        return {"type": "string", "format": "date-time"}
    if kind == "phase":
        return {"enum": PHASES}
    if kind == "predicate_status":
        return {"enum": ["pass", "fail", "not_applicable"]}
    if kind == "positive_integer":
        return {"type": "integer", "minimum": 1}
    if kind == "consumer_lock_readiness_state":
        return {"const": "not_materialized"}
    if kind == "consumer_lock_readiness_reason":
        return {"const": "kernel_build_not_yet_available"}
    if kind == "consumer_lock_profile":
        return {
            "enum": [
                "bootstrap_consumable",
                "execution_eligible",
                "shadow_eligible",
                "publication_eligible",
            ]
        }
    if kind == "reserved_consumer_lock_path":
        return {"const": "governance/authority-consumer.lock.yaml"}
    raise AssertionError(f"unregistered field kind: {kind}")


def receipt_schema(receipt_type: str) -> dict[str, Any]:
    fields = RECEIPT_FIELDS[receipt_type]
    common = {
        "schema_version": {"const": "1.0.0"},
        "receipt_type": {"const": receipt_type},
        "receipt_id": {"$ref": "#/$defs/sha256"},
        "authority_lock_hash": {"$ref": "#/$defs/sha256"},
        "decision": (
            {"const": "not_applicable"}
            if receipt_type == "consumer_lock_readiness"
            else {
                "enum": (
                    ["allow", "deny", "not_applicable"]
                    if receipt_type in NOT_APPLICABLE_RECEIPT_TYPES
                    else ["allow", "deny"]
                )
            }
        ),
        "reason_codes": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "uniqueItems": True,
        },
        "produced_at": {"type": "string", "format": "date-time"},
    }
    properties = {**common, **{name: _schema_for_kind(kind) for name, kind in fields.items()}}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"schema://governance/{receipt_type.replace('_', '-')}/1.0.0",
        "title": TITLES[receipt_type],
        "type": "object",
        "additionalProperties": False,
        "required": list(properties),
        "properties": properties,
        "$defs": {
            "sha256": {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"},
            "git_oid": {"type": "string", "pattern": "^[0-9a-f]{40}$"},
        },
    }


def generate_receipt_schemas(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    for receipt_type in sorted(RECEIPT_FIELDS):
        path = output_dir / f"{receipt_type.replace('_', '-')}.schema.json"
        path.write_text(
            json.dumps(receipt_schema(receipt_type), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _validate_field(kind: FieldKind, value: Any, *, where: str) -> None:
    if kind == "string":
        require_non_empty_string(value, where=where)
    elif kind == "nullable_string":
        if value is not None:
            require_non_empty_string(value, where=where)
    elif kind == "sha256":
        require_sha256(value, where=where)
    elif kind == "git_oid":
        require_commit(value, where=where)
    elif kind == "string_array":
        values = require_list(value, where=where)
        if not all(isinstance(item, str) and item for item in values):
            raise GateViolation("AUTH-RECEIPT-FIELD", f"{where} must contain strings")
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise GateViolation("AUTH-RECEIPT-FIELD", f"{where} must be boolean")
    elif kind == "date_time":
        require_non_empty_string(value, where=where)
    elif kind == "phase":
        if value not in PHASES:
            raise GateViolation("AUTH-RECEIPT-FIELD", f"{where} has unknown phase")
    elif kind == "predicate_status":
        if value not in {"pass", "fail", "not_applicable"}:
            raise GateViolation("AUTH-RECEIPT-FIELD", f"{where} has unknown status")
    elif kind == "positive_integer":
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise GateViolation("AUTH-RECEIPT-FIELD", f"{where} must be a positive integer")
    elif kind == "consumer_lock_readiness_state":
        if value != "not_materialized":
            raise GateViolation("AUTH-RECEIPT-FIELD", f"{where} must be not_materialized")
    elif kind == "consumer_lock_readiness_reason":
        if value != "kernel_build_not_yet_available":
            raise GateViolation(
                "AUTH-RECEIPT-FIELD",
                f"{where} must be kernel_build_not_yet_available",
            )
    elif kind == "consumer_lock_profile":
        if value not in {
            "bootstrap_consumable",
            "execution_eligible",
            "shadow_eligible",
            "publication_eligible",
        }:
            raise GateViolation("AUTH-RECEIPT-FIELD", f"{where} has unknown profile")
    elif kind == "reserved_consumer_lock_path":
        if value != "governance/authority-consumer.lock.yaml":
            raise GateViolation(
                "AUTH-RECEIPT-FIELD", f"{where} must be the reserved consumer lock path"
            )
    else:  # pragma: no cover - registry and generator are co-located
        raise AssertionError(kind)


def validate_typed_receipt(receipt: Mapping[str, Any], *, expected_type: str | None = None) -> None:
    receipt_type = require_non_empty_string(receipt.get("receipt_type"), where="receipt_type")
    if receipt_type not in RECEIPT_FIELDS or (expected_type and receipt_type != expected_type):
        raise GateViolation("AUTH-RECEIPT-TYPE", f"unexpected receipt type: {receipt_type}")
    fields = RECEIPT_FIELDS[receipt_type]
    common = {
        "schema_version",
        "receipt_type",
        "receipt_id",
        "authority_lock_hash",
        "decision",
        "reason_codes",
        "produced_at",
    }
    require_closed(receipt, required=common | fields.keys(), where=TITLES[receipt_type])
    if receipt["schema_version"] != "1.0.0":
        raise GateViolation("AUTH-RECEIPT-VERSION", "unsupported receipt version")
    require_sha256(receipt["receipt_id"], where="receipt_id")
    require_sha256(receipt["authority_lock_hash"], where="authority_lock_hash")
    allowed_decisions = (
        {"not_applicable"}
        if receipt_type == "consumer_lock_readiness"
        else (
            {"allow", "deny", "not_applicable"}
            if receipt_type in NOT_APPLICABLE_RECEIPT_TYPES
            else {"allow", "deny"}
        )
    )
    if receipt["decision"] not in allowed_decisions:
        raise GateViolation(
            "AUTH-RECEIPT-DECISION",
            f"{receipt_type} does not permit decision={receipt['decision']}",
        )
    reason_codes = require_list(receipt["reason_codes"], where="reason_codes")
    if len(reason_codes) != len(set(reason_codes)):
        raise GateViolation("AUTH-RECEIPT-REASONS", "reason codes must be unique")
    require_non_empty_string(receipt["produced_at"], where="produced_at")
    for name, kind in fields.items():
        _validate_field(kind, receipt[name], where=name)
    hash_view = dict(receipt)
    hash_view.pop("receipt_id")
    hash_view.pop("produced_at")
    if canonical_hash(hash_view) != receipt["receipt_id"]:
        raise GateViolation("AUTH-RECEIPT-HASH", "receipt_id does not bind receipt content")
    if receipt["decision"] == "allow" and reason_codes:
        raise GateViolation("AUTH-RECEIPT-ALLOW-REASONS", "allow receipt cannot contain failures")
    if receipt["decision"] == "deny" and not reason_codes:
        raise GateViolation("AUTH-RECEIPT-DENY-REASONS", "deny receipt needs a reason code")
    if receipt_type == "consumer_lock_readiness" and reason_codes:
        raise GateViolation(
            "AUTH-RECEIPT-READINESS-REASONS",
            "not-materialized readiness uses its closed reason field, not failure codes",
        )
    if receipt_type in {"consumer_lock_readiness", "kernel_build", "consumer_lock"}:
        if receipt["authority_lock_hash"] != receipt["authority_bundle_hash"]:
            raise GateViolation(
                "AUTH-RECEIPT-AUTHORITY-MISMATCH",
                "consumer-bound receipt authority hashes differ",
            )


def make_typed_receipt(
    receipt_type: str,
    *,
    authority_lock_hash: str,
    decision: str,
    reason_codes: Sequence[str],
    fields: Mapping[str, Any] | None = None,
    **field_values: Any,
) -> dict[str, Any]:
    if receipt_type not in RECEIPT_FIELDS:
        raise GateViolation("AUTH-RECEIPT-TYPE", f"unknown receipt type: {receipt_type}")
    if fields is not None and field_values:
        raise GateViolation("AUTH-RECEIPT-FIELDS", "use fields or keyword fields, not both")
    payload = dict(fields) if fields is not None else field_values
    body = {
        "schema_version": "1.0.0",
        "receipt_type": receipt_type,
        "authority_lock_hash": authority_lock_hash,
        "decision": decision,
        "reason_codes": list(reason_codes),
        **payload,
    }
    receipt = {**body, "receipt_id": canonical_hash(body), "produced_at": utc_now()}
    validate_typed_receipt(receipt, expected_type=receipt_type)
    return receipt
