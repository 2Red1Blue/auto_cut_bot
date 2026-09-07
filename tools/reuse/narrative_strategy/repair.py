"""NarrativeRepairRequest/v1: budgeted local repair for failed shadow drafts.

Rules from doc 01-r1-narrative-strategy.md:
- only the failed proposal/partition is repaired; the whole stage artifact set
  is re-validated after the repair returns;
- identical input + identical error fingerprint never re-calls the provider;
- mechanical problems (enum ordering, ID recovery) are local reprocess jobs,
  never provider calls;
- the repair request carries the original raw hash, bounded error paths, the
  legal short references, bounded local context and the remaining budget.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

REPAIR_SCHEMA = "NarrativeRepairRequest/v1"

# error codes repairable by a provider call; everything else is local reprocess
PROVIDER_REPAIRABLE_CODES = frozenset({
    "COMPACT_FIELD_INVALID",
    "COMPACT_REFERENCE_NOT_FOUND",
    "COMPACT_REFERENCE_TYPE_MISMATCH",
    "COMPACT_DUPLICATE_REFERENCE",
    "COMPACT_SOURCE_SELECTION_INVALID",
    "COMPACT_EDITING_PROFILE_NOT_FOUND",
    "NARRATIVE_DUTY_UNKNOWN_KIND",
    "NARRATIVE_DUTY_TARGET_MISMATCH",
    "NARRATIVE_DUTY_REFERENCE_NOT_FOUND",
    "NARRATIVE_DUTY_REFERENCE_TYPE_MISMATCH",
    "NARRATIVE_DUTY_SETUP_PAYOFF_IDENTICAL",
    "NARRATIVE_DUTY_ORDER_REVERSED",
    "NARRATIVE_DUTY_DUPLICATE",
    "NARRATIVE_DUTY_FIELD_INVALID",
})

# purely mechanical failures: fix locally, zero provider calls
MECHANICAL_CODES = frozenset({
    "COMPACT_JSON_INVALID",
    "COMPACT_BUDGET_EXCEEDED",
    "COMPACT_SCHEMA_UNSUPPORTED",
})

MAX_ERROR_PATHS = 20


@dataclass(frozen=True)
class RepairBudget:
    max_calls: int
    max_input_tokens: int
    max_output_tokens: int

    def to_mapping(self) -> dict[str, int]:
        return {
            "max_calls": self.max_calls,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
        }


@dataclass(frozen=True)
class NarrativeRepairRequest:
    schema_version: str
    original_attempt_id: str
    original_raw_sha256: str
    error_fingerprint: str
    error_paths: tuple[str, ...]
    legal_refs: tuple[str, ...]
    local_context_bytes: int
    remaining_budget: RepairBudget
    extra: dict[str, Any] = field(default_factory=dict)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema_version": REPAIR_SCHEMA,
            "original_attempt_id": self.original_attempt_id,
            "original_raw_sha256": self.original_raw_sha256,
            "error_fingerprint": self.error_fingerprint,
            "error_paths": list(self.error_paths),
            "legal_refs": list(self.legal_refs),
            "local_context_bytes": self.local_context_bytes,
            "remaining_budget": self.remaining_budget.to_mapping(),
            "extra": self.extra,
        }


def error_fingerprint(raw_sha256: str, diagnostics: list[dict[str, Any]]) -> str:
    """Stable fingerprint of (input identity, sorted error signatures).

    The same input failing with the same errors must never trigger a second
    provider call — dedup happens on this value."""
    material = {
        "raw_sha256": raw_sha256,
        "errors": sorted(
            json.dumps({k: d.get(k) for k in ("code", "json_path", "proposal_index")},
                       sort_keys=True, ensure_ascii=False)
            for d in diagnostics
        ),
    }
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def classify(diagnostics: list[dict[str, Any]]) -> str:
    """'provider_repair' | 'local_reprocess' | 'unrecoverable'."""
    codes = {d.get("code") for d in diagnostics}
    if not codes:
        return "local_reprocess"  # nothing failed
    if codes & MECHANICAL_CODES:
        return "local_reprocess"
    if codes <= PROVIDER_REPAIRABLE_CODES:
        return "provider_repair"
    return "unrecoverable"


def build_repair_request(
    *,
    original_attempt_id: str,
    original_raw: str,
    diagnostics: list[dict[str, Any]],
    legal_refs: list[str],
    local_context: bytes,
    spent_calls: int,
    budget: RepairBudget,
) -> NarrativeRepairRequest | None:
    """Build the repair request, or None when repair must not re-call the provider."""
    action = classify(diagnostics)
    if action != "provider_repair":
        return None
    remaining_calls = budget.max_calls - spent_calls
    if remaining_calls <= 0:
        return None
    raw_sha = hashlib.sha256(original_raw.encode("utf-8")).hexdigest()
    paths = tuple(
        d.get("json_path") for d in diagnostics
        if isinstance(d.get("json_path"), str)
    )[:MAX_ERROR_PATHS]
    return NarrativeRepairRequest(
        schema_version=REPAIR_SCHEMA,
        original_attempt_id=original_attempt_id,
        original_raw_sha256=raw_sha,
        error_fingerprint=error_fingerprint(raw_sha, diagnostics),
        error_paths=paths,  # type: ignore[arg-type]
        legal_refs=tuple(legal_refs),
        local_context_bytes=len(local_context),
        remaining_budget=RepairBudget(
            max_calls=remaining_calls,
            max_input_tokens=budget.max_input_tokens,
            max_output_tokens=budget.max_output_tokens,
        ),
    )
