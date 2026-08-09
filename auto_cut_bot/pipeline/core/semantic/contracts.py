"""合约结果类型。

只包含 dataclass 定义。
这些类型被 JobResponseValidation 和验证/修复模块用作类型标注。
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from typing import Any


def _json_sha256(value: Any) -> str:
    """Deterministic JSON SHA-256 — 替代 story_common.json_sha256。"""
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


# ── series_assignment_contract ──────────────────────────────────────────

@dataclass(frozen=True)
class AssignmentContractResult:
    """Result of validating and optionally canonicalizing one assignment."""

    effective_assignment: dict[str, Any]
    repairs: list[dict[str, Any]]
    errors: list[str]
    raw_sha256: str
    effective_sha256: str


@dataclass(frozen=True)
class GlobalAssignmentGraphResult:
    """Result of canonicalizing Beat identities and dependencies globally."""

    effective_assignments: list[dict[str, Any]]
    repairs: list[dict[str, Any]]
    errors: list[str]
    raw_sha256: str
    effective_sha256: str


# ── series_registry_admission ───────────────────────────────────────────

@dataclass(frozen=True)
class SeriesRegistryAdmissionResult:
    raw_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    admission: dict[str, Any]
    quarantine: dict[str, Any]
    validation: dict[str, Any]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors and self.validation.get("ok") is True

    @property
    def status(self) -> str:
        return str(self.admission.get("status") or "blocked")

    @property
    def raw_sha256(self) -> str:
        return _json_sha256(self.raw_registry)

    @property
    def effective_sha256(self) -> str:
        return _json_sha256(self.effective_registry)

    @property
    def repairs(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            copy.deepcopy(self.admission.get("local_admission_actions", []))
        )


# ── series_registry_alias_repair ────────────────────────────────────────

@dataclass(frozen=True)
class SeriesRegistryAliasRepairResult:
    raw_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    repairs: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def raw_sha256(self) -> str:
        return _json_sha256(self.raw_registry)

    @property
    def effective_sha256(self) -> str:
        return _json_sha256(self.effective_registry)

    def as_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "policy_version": "series-registry-alias-deletion-repair-v1",
            "status": "repaired" if self.ok else "partially_repaired",
            "raw_registry_sha256": self.raw_sha256,
            "effective_registry_sha256": self.effective_sha256,
            "repair_count": len(self.repairs),
            "repairs": list(self.repairs),
            "blocking_errors": list(self.errors),
        }


# ── series_registry_identity_repair ─────────────────────────────────────

@dataclass(frozen=True)
class SeriesRegistryIdentityRepairResult:
    raw_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    repairs: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def raw_sha256(self) -> str:
        return _json_sha256(self.raw_registry)

    @property
    def effective_sha256(self) -> str:
        return _json_sha256(self.effective_registry)

    def as_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "policy_version": "series-registry-identity-repair-v1",
            "status": "repaired" if self.ok else "blocked",
            "raw_registry_sha256": self.raw_sha256,
            "effective_registry_sha256": self.effective_sha256,
            "repair_count": len(self.repairs),
            "repairs": list(self.repairs),
            "decisions": list(self.decisions),
            "blocking_errors": list(self.errors),
        }


# ── series_registry_recovery ────────────────────────────────────────────

@dataclass(frozen=True)
class RegistryRecoveryMergeResult:
    incumbent_registry: dict[str, Any]
    incoming_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    imported_relationships: tuple[dict[str, Any], ...]
    skipped_relationships: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    uncovered_before: tuple[str, ...]
    uncovered_after: tuple[str, ...]

    @property
    def progressed(self) -> bool:
        return (
            bool(self.imported_relationships)
            and set(self.uncovered_after) < set(self.uncovered_before)
        )

    def as_audit(self) -> dict[str, Any]:
        return {
            "policy_version": "series-registry-recovery-v3-monotonic-delta",
            "incumbent_registry_sha256": _json_sha256(self.incumbent_registry),
            "incoming_registry_sha256": _json_sha256(self.incoming_registry),
            "effective_registry_sha256": _json_sha256(self.effective_registry),
            "uncovered_before": list(self.uncovered_before),
            "uncovered_after": list(self.uncovered_after),
            "imported_relationships": list(self.imported_relationships),
            "skipped_relationships": list(self.skipped_relationships),
            "errors": list(self.errors),
        }


# ── series_registry_reference_repair ────────────────────────────────────

@dataclass(frozen=True)
class SeriesRegistryReferenceRepairResult:
    raw_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    repairs: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]

    @property
    def raw_sha256(self) -> str:
        return _json_sha256(self.raw_registry)

    @property
    def effective_sha256(self) -> str:
        return _json_sha256(self.effective_registry)

    def as_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "policy_version": "series-registry-reference-deletion-repair-v1",
            "status": "repaired" if not self.errors else "partially_repaired",
            "raw_registry_sha256": self.raw_sha256,
            "effective_registry_sha256": self.effective_sha256,
            "repair_count": len(self.repairs),
            "repairs": list(self.repairs),
            "blocking_errors": list(self.errors),
        }


# ── series_registry_relationship_repair ─────────────────────────────────

@dataclass(frozen=True)
class SeriesRegistryRelationshipRepairResult:
    raw_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    repairs: tuple[dict[str, Any], ...]
    decisions: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.errors

    @property
    def raw_sha256(self) -> str:
        return _json_sha256(self.raw_registry)

    @property
    def effective_sha256(self) -> str:
        return _json_sha256(self.effective_registry)

    def as_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "policy_version": "series-registry-relationship-semantic-repair-v3",
            "status": "repaired" if self.ok else "blocked",
            "raw_registry_sha256": self.raw_sha256,
            "effective_registry_sha256": self.effective_sha256,
            "repair_count": len(self.repairs),
            "repairs": list(self.repairs),
            "decisions": list(self.decisions),
            "blocking_errors": list(self.errors),
        }


# ── window_analysis_contract ────────────────────────────────────────────

@dataclass(frozen=True)
class WindowAnalysisContractResult:
    """Canonical Window value plus typed repair/audit findings."""

    effective_window: dict[str, Any]
    repairs: list[dict[str, Any]]
    blockers: list[dict[str, Any]]
    errors: list[str]
    raw_sha256: str
    effective_sha256: str
    quality_status: str