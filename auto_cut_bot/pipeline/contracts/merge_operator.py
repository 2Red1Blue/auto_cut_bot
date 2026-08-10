"""Deterministic merge operator for multi-source conflict resolution.

This module implements a zero-LLM merge algorithm that combines two data
sources (e.g. API output and LLM output) into a single canonical record.
The algorithm is deterministic, traceable, and produces a full provenance
audit trail.

Algorithm
---------
For each field present in either source:
    1. Both empty (None, empty string, empty list) — skip, not included.
    2. One empty, the other non-empty — use the non-empty value, mark as
       auto_resolved with source trace.
    3. Both non-empty and equal — use the value, mark as auto_resolved.
    4. Both non-empty and different — classify the field via
       field_registry.classify_field() and apply the category strategy:

       ==================== ============ ==============================
       Category             Strategy     Resolution
       ==================== ============ ==============================
       measurable           prefer API   auto_resolved (api wins)
       api_unique           prefer API   auto_resolved (api wins)
       author_intent        prefer LLM   conflict (pending review)
       video_verifiable     flag VLM     conflict (high_severity)
       ==================== ============ ==============================

The result is a MergeResult containing:
    - canonical: the merged dict with resolved values
    - provenance: list of FieldProvenance records tracing each field
    - conflicts: list of FieldConflict records for fields needing review

Usage
-----
    from autocut_core.pipeline.contracts.merge_operator import merge
    from autocut_core.pipeline.contracts.field_registry import classify_field

    src_a = {"book_name": "The Journey", "duration": 120.0, "genre": "romance"}
    src_b = {"book_name": "The Journey", "duration": 125.0, "genre": "fantasy"}

    result = merge(
        source_a=src_a,
        source_b=src_b,
        source_a_label="api",
        source_b_label="llm",
        table="episodes",
    )
    # result.canonical: {"book_name": "The Journey", "duration": 120.0, "genre": "fantasy"}
    # result.conflicts: [FieldConflict(field="genre", ...)]
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .field_registry import (
    CATEGORY_AUTHOR_INTENT,
    CATEGORY_VIDEO_VERIFIABLE,
    classify_field,
    get_preferred_source,
)


class ResolutionStatus(str, Enum):
    """How a field conflict was resolved."""

    AUTO_RESOLVED = "auto_resolved"
    """No conflict; the value was determined automatically."""

    PENDING = "pending"
    """Conflict requires LLM review (author_intent category)."""

    HIGH_SEVERITY = "high_severity"
    """Conflict requires VLM verification (video_verifiable category)."""


class ConflictSeverity(str, Enum):
    """Severity level for a conflict."""

    INFO = "info"
    WARNING = "warning"
    HIGH = "high"


@dataclass
class FieldProvenance:
    """Trace record for a single field's merge decision.

    Attributes
    ----------
    field : str
        The field name.
    table : str
        The table name.
    category : str
        The field's classification category.
    resolution : ResolutionStatus
        How the field was resolved.
    winner : str | None
        Which source provided the winning value (source_a_label or source_b_label).
    reason : str
        Human-readable explanation of the decision.
    """

    field: str
    table: str
    category: str
    resolution: ResolutionStatus
    winner: str | None
    reason: str


@dataclass
class FieldConflict:
    """A conflict that could not be auto-resolved.

    Attributes
    ----------
    field : str
        The field name.
    table : str
        The table name.
    category : str
        The field's classification category.
    severity : ConflictSeverity
        How urgent this conflict is.
    value_a : Any
        Value from source A.
    value_b : Any
        Value from source B.
    preferred_source : str
        Which source's value is preferred by strategy.
    suggested_action : str
        Recommended next step to resolve the conflict.
    """

    field: str
    table: str
    category: str
    severity: ConflictSeverity
    value_a: Any
    value_b: Any
    preferred_source: str
    suggested_action: str


@dataclass
class MergeResult:
    """The complete result of a merge operation.

    Attributes
    ----------
    canonical : dict[str, Any]
        The merged record. Contains only fields that have a resolved value.
        Fields with pending conflicts are populated with the preferred value
        from the strategy (LLM for author_intent, API for measurable, etc.).
    provenance : list[FieldProvenance]
        Provenance records for every field that was resolved.
    conflicts : list[FieldConflict]
        Conflicts that require human or VLM review.
    table : str
        The table this merge applies to.
    """

    canonical: dict[str, Any]
    provenance: list[FieldProvenance] = field(default_factory=list)
    conflicts: list[FieldConflict] = field(default_factory=list)
    table: str = ""

    @property
    def has_conflicts(self) -> bool:
        """True if there are unresolved conflicts."""
        return len(self.conflicts) > 0

    @property
    def has_high_severity(self) -> bool:
        """True if any conflict is high severity."""
        return any(c.severity == ConflictSeverity.HIGH for c in self.conflicts)

    @property
    def conflict_count(self) -> int:
        """Number of pending conflicts."""
        return len(self.conflicts)

    @property
    def auto_resolved_count(self) -> int:
        """Number of auto-resolved fields."""
        return sum(
            1 for p in self.provenance if p.resolution == ResolutionStatus.AUTO_RESOLVED
        )


def _is_empty(value: Any) -> bool:
    """Check if a value is semantically empty."""
    if value is None:
        return True
    if isinstance(value, str) and value.strip() == "":
        return True
    if isinstance(value, (list, dict)) and len(value) == 0:
        return True
    return False


def _values_equal(a: Any, b: Any) -> bool:
    """Compare two values for equality.

    Handles lists, dicts, and scalar types.
    """
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(
            _values_equal(ai, bi) for ai, bi in zip(a, b)
        )
    if isinstance(a, dict) and isinstance(b, dict):
        if a.keys() != b.keys():
            return False
        return all(
            _values_equal(a[k], b[k]) for k in a
        )
    return a == b


def _conflict_severity(category: str) -> ConflictSeverity:
    """Map field category to conflict severity."""
    if category == CATEGORY_VIDEO_VERIFIABLE:
        return ConflictSeverity.HIGH
    if category == CATEGORY_AUTHOR_INTENT:
        return ConflictSeverity.WARNING
    return ConflictSeverity.INFO


def _suggested_action(category: str, field: str, table: str) -> str:
    """Generate a human-readable suggested action for a conflict."""
    if category == CATEGORY_VIDEO_VERIFIABLE:
        return (
            f"Field '{field}' in '{table}' requires VLM verification. "
            "Run video analysis on the relevant segment to confirm the ground truth."
        )
    if category == CATEGORY_AUTHOR_INTENT:
        return (
            f"Field '{field}' in '{table}' has semantic disagreement. "
            "Review both values and select the one that best captures author intent."
        )
    return (
        f"Field '{field}' in '{table}' has a conflict. "
        "Manual review recommended."
    )


def merge(
    source_a: dict[str, Any],
    source_b: dict[str, Any],
    source_a_label: str,
    source_b_label: str,
    table: str,
    *,
    mode: str = "auto",
) -> MergeResult:
    """Merge two source dictionaries into a canonical record.

    Parameters
    ----------
    source_a : dict[str, Any]
        First source record (typically API data).
    source_b : dict[str, Any]
        Second source record (typically LLM output).
    source_a_label : str
        Label for the first source, e.g. "api", "llm", "vlm".
    source_b_label : str
        Label for the second source, e.g. "api", "llm", "vlm".
    table : str
        The table name.
    mode : str
        "auto" — Agent resolves conflicts (default). Only video_verifiable
        conflicts are flagged for review; author_intent conflicts are
        auto-resolved with LLM preferred.
        "manual" — All conflicts pause for human review.

    Returns
    -------
    MergeResult
        The merged result with canonical dict, provenance, and conflicts.
    """
    all_fields: set[str] = set(source_a.keys()) | set(source_b.keys())
    canonical: dict[str, Any] = {}
    provenance: list[FieldProvenance] = []
    conflicts: list[FieldConflict] = []

    for field in sorted(all_fields):
        value_a = source_a.get(field)
        value_b = source_b.get(field)

        # ── Step 1: Both empty — skip ─────────────────────────────────────
        if _is_empty(value_a) and _is_empty(value_b):
            continue

        # ── Step 2: One empty — use the non-empty value ───────────────────
        if _is_empty(value_a) and not _is_empty(value_b):
            canonical[field] = value_b
            provenance.append(
                FieldProvenance(
                    field=field,
                    table=table,
                    category=classify_field(table, field),
                    resolution=ResolutionStatus.AUTO_RESOLVED,
                    winner=source_b_label,
                    reason=f"Only {source_b_label} provided a value for '{field}'",
                )
            )
            continue

        if not _is_empty(value_a) and _is_empty(value_b):
            canonical[field] = value_a
            provenance.append(
                FieldProvenance(
                    field=field,
                    table=table,
                    category=classify_field(table, field),
                    resolution=ResolutionStatus.AUTO_RESOLVED,
                    winner=source_a_label,
                    reason=f"Only {source_a_label} provided a value for '{field}'",
                )
            )
            continue

        # ── Step 3: Both non-empty and equal — use it ─────────────────────
        if _values_equal(value_a, value_b):
            canonical[field] = value_a
            provenance.append(
                FieldProvenance(
                    field=field,
                    table=table,
                    category=classify_field(table, field),
                    resolution=ResolutionStatus.AUTO_RESOLVED,
                    winner="both",
                    reason=f"Both sources agree on '{field}'",
                )
            )
            continue

        # ── Step 4: Both non-empty and different — classify field ─────────
        category = classify_field(table, field)
        preferred = get_preferred_source(category)

        # Determine which source's value is preferred
        preferred_value: Any
        preferred_label: str
        other_value: Any
        other_label: str

        if (preferred == "api" and source_a_label == "api") or (
            preferred == "llm" and source_a_label == "llm"
        ) or (preferred == "vlm" and source_a_label == "vlm"):
            preferred_value = value_a
            preferred_label = source_a_label
            other_value = value_b
            other_label = source_b_label
        elif (preferred == "api" and source_b_label == "api") or (
            preferred == "llm" and source_b_label == "llm"
        ) or (preferred == "vlm" and source_b_label == "vlm"):
            preferred_value = value_b
            preferred_label = source_b_label
            other_value = value_a
            other_label = source_a_label
        else:
            # Neither source matches the preferred type — use source_a as
            # fallback and flag a conflict
            preferred_value = value_a
            preferred_label = source_a_label
            other_value = value_b
            other_label = source_b_label

        if category in (CATEGORY_AUTHOR_INTENT, CATEGORY_VIDEO_VERIFIABLE):
            severity = _conflict_severity(category)

            if mode == "auto" and category == CATEGORY_AUTHOR_INTENT:
                # AUTO mode: Agent resolves author_intent conflicts automatically.
                # LLM is preferred, API value is logged as provenance.
                canonical[field] = preferred_value
                provenance.append(
                    FieldProvenance(
                        field=field, table=table, category=category,
                        resolution=ResolutionStatus.AUTO_RESOLVED,
                        winner=preferred_label,
                        reason=(
                            f"'{field}' differs but AUTO mode auto-resolves "
                            f"author_intent with {preferred_label} preferred."
                        ),
                    )
                )
                # Still log as info-level conflict for Agent review
                conflicts.append(
                    FieldConflict(
                        field=field, table=table, category=category,
                        severity=ConflictSeverity.INFO,
                        value_a=value_a, value_b=value_b,
                        preferred_source=preferred,
                        suggested_action=f"Agent auto-resolved: {preferred_label} preferred.",
                    )
                )
            else:
                # MANUAL mode or VIDEO_VERIFIABLE: create pending conflict
                resolution_status = (
                    ResolutionStatus.HIGH_SEVERITY
                    if severity == ConflictSeverity.HIGH
                    else ResolutionStatus.PENDING
                )
                canonical[field] = preferred_value
                conflict = FieldConflict(
                    field=field, table=table, category=category,
                    severity=severity, value_a=value_a, value_b=value_b,
                    preferred_source=preferred,
                    suggested_action=_suggested_action(category, field, table),
                )
                conflicts.append(conflict)
                provenance.append(
                    FieldProvenance(
                        field=field, table=table, category=category,
                        resolution=resolution_status, winner=preferred_label,
                        reason=(
                            f"'{field}' differs between sources. "
                            f"Category '{category}' requires review. "
                            f"Tentatively using {preferred_label} value."
                        ),
                    )
                )
        else:
            # measurable and api_unique: auto-resolve with preferred source
            canonical[field] = preferred_value
            provenance.append(
                FieldProvenance(
                    field=field,
                    table=table,
                    category=category,
                    resolution=ResolutionStatus.AUTO_RESOLVED,
                    winner=preferred_label,
                    reason=(
                        f"'{field}' differs between sources. "
                        f"Category '{category}' prefers {preferred_label}. "
                        f"Auto-resolved."
                    ),
                )
            )

    return MergeResult(
        canonical=canonical,
        provenance=provenance,
        conflicts=conflicts,
        table=table,
    )


def merge_batch(
    pairs: list[tuple[dict[str, Any], dict[str, Any]]],
    source_a_label: str,
    source_b_label: str,
    table: str,
) -> list[MergeResult]:
    """Merge multiple pairs of source records.

    Parameters
    ----------
    pairs : list[tuple[dict[str, Any], dict[str, Any]]]
        List of (source_a, source_b) tuples.
    source_a_label : str
        Label for the first source.
    source_b_label : str
        Label for the second source.
    table : str
        The table name.

    Returns
    -------
    list[MergeResult]
        One MergeResult per pair.
    """
    return [
        merge(a, b, source_a_label, source_b_label, table)
        for a, b in pairs
    ]


def merge_summary(result: MergeResult) -> dict[str, Any]:
    """Produce a human-readable summary of a merge result.

    Parameters
    ----------
    result : MergeResult
        The merge result to summarize.

    Returns
    -------
    dict[str, Any]
        Summary with counts and conflict details.
    """
    return {
        "table": result.table,
        "total_fields_resolved": len(result.canonical),
        "auto_resolved": result.auto_resolved_count,
        "conflicts_total": result.conflict_count,
        "has_high_severity": result.has_high_severity,
        "conflicts": [
            {
                "field": c.field,
                "category": c.category,
                "severity": c.severity.value,
                "preferred_source": c.preferred_source,
                "suggested_action": c.suggested_action,
            }
            for c in result.conflicts
        ],
    }


__all__ = [
    "ResolutionStatus",
    "ConflictSeverity",
    "FieldProvenance",
    "FieldConflict",
    "MergeResult",
    "merge",
    "merge_batch",
    "merge_summary",
    "agent_resolve",
]


def agent_resolve(
    result: MergeResult,
    field: str,
    decision: str,
    *,
    reason: str = "",
) -> MergeResult:
    """Agent resolves a specific conflict by overriding the canonical value.

    Called by the Agent when it wants to override the auto-resolved value.
    This is the agent-native resolution path — Agent reviews conflicts
    and makes decisions, rather than blocking for human input.

    Parameters
    ----------
    result : MergeResult
        The merge result containing conflicts.
    field : str
        The field name to resolve.
    decision : str
        "source_a" or "source_b" — which source's value to use.
    reason : str
        Agent's reasoning for the decision.

    Returns
    -------
    MergeResult
        Updated result with the conflict resolved.
    """
    # Update canonical value
    for conflict in result.conflicts:
        if conflict.field == field:
            if decision == "source_a":
                result.canonical[field] = conflict.value_a
            elif decision == "source_b":
                result.canonical[field] = conflict.value_b
            # Update provenance
            result.provenance.append(
                FieldProvenance(
                    field=field,
                    table=result.table,
                    category=conflict.category,
                    resolution=ResolutionStatus.AUTO_RESOLVED,
                    winner=f"agent_resolved:{decision}",
                    reason=f"Agent resolved: {reason}",
                )
            )
            break

    # Remove resolved conflict from list
    result.conflicts = [c for c in result.conflicts if c.field != field]
    return result