#!/usr/bin/env python3
"""Deterministic admission and repair policy for Window Analysis output.

Window Analysis contains two different kinds of data:

* core semantic evidence (timeline segments, story beats, visual events);
* optional observations and edit suggestions (dialogue/text and candidates).

Core evidence remains blocking when its time range cannot be trusted. Optional
records are quarantined item-by-item instead of rejecting the whole Window.
Teaser duration is an eligibility contract, not an admission contract: an
overlong Highlight remains valid evidence but is excluded later by the shared
Teaser eligibility predicate.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any

from autocut_core.io import json_sha256
from autocut_core.schema.compat import WINDOW_ANALYSIS_SCHEMA, validate_schema
from autocut_core.libs.teaser import TEASER_MAXIMUM_SECONDS


POLICY_VERSION = "window-analysis-admission-v1"

CORE_INTERVAL_FIELDS = (
    "timeline_segments",
    "story_beats",
    "visual_events",
)
OPTIONAL_INTERVAL_FIELDS = (
    "dialogue_and_text",
    "candidates",
)
ALL_INTERVAL_FIELDS = CORE_INTERVAL_FIELDS + OPTIONAL_INTERVAL_FIELDS
BOUNDARY_TOLERANCE_SECONDS = 0.05
OPTIONAL_ITEM_SCHEMAS = {
    field: WINDOW_ANALYSIS_SCHEMA["properties"][field]["items"]
    for field in OPTIONAL_INTERVAL_FIELDS
}


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


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _coerce_number(value: Any) -> float | None:
    if _is_number(value):
        return float(value)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None


def _blocking_error(code: str, path: str, detail: str) -> str:
    return f"{code}: {path}: {detail}"


def _blocking_finding(
    *,
    code: str,
    path: str,
    original: Any,
    detail: str,
    window_start: float | None,
    window_end: float | None,
    repair_route: str | None = None,
) -> dict[str, Any]:
    finding = {
        "code": code,
        "path": path,
        "criticality": "core",
        "repairability": (
            "physical_window_recovery"
            if repair_route == "local_window_media"
            else "semantic_regeneration"
        ),
        "scope": "item" if path != "job" else "job",
        "action": (
            "retry_with_local_window_media"
            if repair_route == "local_window_media"
            else "reject_window"
        ),
        "original": original,
        "detail": detail,
    }
    if window_start is not None and window_end is not None:
        finding["declared_window"] = {
            "start": window_start,
            "end": window_end,
        }
    if repair_route is not None:
        finding["repair_route"] = repair_route
    return finding


def _core_repair_route(
    *,
    job: dict[str, Any],
    code: str,
    item_start: float | None,
    item_end: float | None,
    window_end: float,
) -> str | None:
    """Return the narrowly allowed recovery route for a core interval.

    A physical Window can remove information outside the declared interval,
    but it cannot repair malformed schema, identity, or arbitrary time ranges.
    Zero-length core intervals are eligible only when the model collapsed an
    out-of-window observation exactly onto the upper Window boundary.
    """

    if job.get("media_url_mode") != "full_source" or not job.get("media_url"):
        return None
    if code == "CORE_OUT_OF_WINDOW":
        return "local_window_media"
    if (
        code == "CORE_INVALID_TIME_RANGE"
        and item_start is not None
        and item_end is not None
        and abs(item_start - window_end) <= BOUNDARY_TOLERANCE_SECONDS
        and abs(item_end - window_end) <= BOUNDARY_TOLERANCE_SECONDS
    ):
        return "local_window_media"
    return None


def supports_local_window_media_recovery(
    result: WindowAnalysisContractResult,
) -> bool:
    """Whether every blocking Window finding has the same safe media route."""

    return bool(result.blockers) and all(
        item.get("repair_route") == "local_window_media"
        for item in result.blockers
    )


def _finding(
    *,
    code: str,
    path: str,
    criticality: str,
    repairability: str,
    action: str,
    original: Any,
    effective: Any = None,
    detail: str = "",
) -> dict[str, Any]:
    result = {
        "code": code,
        "path": path,
        "criticality": criticality,
        "repairability": repairability,
        "scope": "item",
        "action": action,
        "original": original,
    }
    if effective is not None:
        result["effective"] = effective
    if detail:
        result["detail"] = detail
    return result


def canonicalize_window_analysis(
    value: dict[str, Any],
    *,
    job: dict[str, Any],
) -> WindowAnalysisContractResult:
    """Apply safe, idempotent Window admission rules.

    No semantic content is invented. The only value-changing actions are:

    * exact numeric-string conversion for interval boundaries;
    * snapping a boundary drift of at most 50 ms to the declared Window;
    * replacing a malformed optional collection with an empty collection;
    * quarantining an invalid optional observation/candidate;
    * removing an exact duplicate optional record.

    Overlong Highlights are deliberately preserved. Their finding records that
    they are not Teaser-eligible; downstream code already uses
    ``is_teaser_eligible_highlight`` to select legal Teaser candidates.
    """

    raw_sha256 = json_sha256(value)
    effective = copy.deepcopy(value)
    repairs: list[dict[str, Any]] = []
    blockers: list[dict[str, Any]] = []
    errors: list[str] = []

    window_start = _coerce_number(job.get("start"))
    window_end = _coerce_number(job.get("end"))
    if (
        window_start is None
        or window_end is None
        or window_end <= window_start
    ):
        detail = f"start={job.get('start')!r}, end={job.get('end')!r}"
        errors.append(_blocking_error("WINDOW_JOB_INVALID_RANGE", "job", detail))
        blockers.append(
            _blocking_finding(
                code="WINDOW_JOB_INVALID_RANGE",
                path="job",
                original={"start": job.get("start"), "end": job.get("end")},
                detail=detail,
                window_start=window_start,
                window_end=window_end,
            )
        )
        return WindowAnalysisContractResult(
            effective_window=effective,
            repairs=repairs,
            blockers=blockers,
            errors=errors,
            raw_sha256=raw_sha256,
            effective_sha256=json_sha256(effective),
            quality_status="blocked",
        )

    for field in ALL_INTERVAL_FIELDS:
        items = effective.get(field)
        if not isinstance(items, list):
            if field in OPTIONAL_INTERVAL_FIELDS:
                repairs.append(
                    _finding(
                        code="OPTIONAL_COLLECTION_INVALID",
                        path=field,
                        criticality="optional",
                        repairability="deterministic",
                        action="replace_with_empty_collection",
                        original=items,
                        effective=[],
                        detail="optional collection must be an array",
                    )
                )
                effective[field] = []
            # Static schema validation owns core collection-shape failures.
            continue
        optional = field in OPTIONAL_INTERVAL_FIELDS
        kept: list[Any] = []
        seen_optional: set[str] = set()
        for index, item in enumerate(items):
            path = f"{field}[{index}]"
            if not isinstance(item, dict):
                if optional:
                    repairs.append(
                        _finding(
                            code="OPTIONAL_ITEM_NOT_OBJECT",
                            path=path,
                            criticality="optional",
                            repairability="deterministic",
                            action="quarantine_item",
                            original=item,
                            detail="optional time-coded record must be an object",
                        )
                    )
                    continue
                detail = "core time-coded record must be an object"
                errors.append(
                    _blocking_error("CORE_ITEM_NOT_OBJECT", path, detail)
                )
                blockers.append(
                    _blocking_finding(
                        code="CORE_ITEM_NOT_OBJECT",
                        path=path,
                        original=item,
                        detail=detail,
                        window_start=window_start,
                        window_end=window_end,
                    )
                )
                kept.append(item)
                continue

            original_item = copy.deepcopy(item)
            item_start = _coerce_number(item.get("start"))
            item_end = _coerce_number(item.get("end"))
            if (
                item_start is None
                or item_end is None
                or item_end <= item_start
            ):
                if optional:
                    repairs.append(
                        _finding(
                            code="OPTIONAL_INVALID_TIME_RANGE",
                            path=path,
                            criticality="optional",
                            repairability="deterministic",
                            action="quarantine_item",
                            original=original_item,
                            detail=(
                                f"start={item.get('start')!r}, "
                                f"end={item.get('end')!r}"
                            ),
                        )
                    )
                    continue
                detail = (
                    f"start={item.get('start')!r}, "
                    f"end={item.get('end')!r}"
                )
                repair_route = _core_repair_route(
                    job=job,
                    code="CORE_INVALID_TIME_RANGE",
                    item_start=item_start,
                    item_end=item_end,
                    window_end=window_end,
                )
                errors.append(
                    _blocking_error("CORE_INVALID_TIME_RANGE", path, detail)
                )
                blockers.append(
                    _blocking_finding(
                        code="CORE_INVALID_TIME_RANGE",
                        path=path,
                        original=original_item,
                        detail=detail,
                        window_start=window_start,
                        window_end=window_end,
                        repair_route=repair_route,
                    )
                )
                kept.append(item)
                continue

            normalized_item = copy.deepcopy(item)
            changed = False
            for key, number in (("start", item_start), ("end", item_end)):
                if not _is_number(item.get(key)):
                    normalized_item[key] = number
                    changed = True

            if (
                item_start < window_start
                and item_start >= window_start - BOUNDARY_TOLERANCE_SECONDS
            ):
                normalized_item["start"] = window_start
                item_start = window_start
                changed = True
            if (
                item_end > window_end
                and item_end <= window_end + BOUNDARY_TOLERANCE_SECONDS
            ):
                normalized_item["end"] = window_end
                item_end = window_end
                changed = True

            if item_start < window_start or item_end > window_end:
                if optional:
                    repairs.append(
                        _finding(
                            code="OPTIONAL_OUT_OF_WINDOW",
                            path=path,
                            criticality="optional",
                            repairability="deterministic",
                            action="quarantine_item",
                            original=original_item,
                            detail=(
                                f"declared_window=[{window_start:.3f}, "
                                f"{window_end:.3f}]"
                            ),
                        )
                    )
                    continue
                detail = (
                    f"range=[{item_start:.3f}, {item_end:.3f}], "
                    f"window=[{window_start:.3f}, {window_end:.3f}]"
                )
                repair_route = _core_repair_route(
                    job=job,
                    code="CORE_OUT_OF_WINDOW",
                    item_start=item_start,
                    item_end=item_end,
                    window_end=window_end,
                )
                errors.append(
                    _blocking_error("CORE_OUT_OF_WINDOW", path, detail)
                )
                blockers.append(
                    _blocking_finding(
                        code="CORE_OUT_OF_WINDOW",
                        path=path,
                        original=original_item,
                        detail=detail,
                        window_start=window_start,
                        window_end=window_end,
                        repair_route=repair_route,
                    )
                )
                kept.append(item)
                continue

            if changed:
                repairs.append(
                    _finding(
                        code="NORMALIZE_TIME_RANGE",
                        path=path,
                        criticality="core" if not optional else "optional",
                        repairability="deterministic",
                        action="normalize_numeric_or_boundary",
                        original=original_item,
                        effective=normalized_item,
                    )
                )

            if optional:
                item_schema_errors = validate_schema(
                    normalized_item,
                    OPTIONAL_ITEM_SCHEMAS[field],
                    where=path,
                )
                if item_schema_errors:
                    repairs.append(
                        _finding(
                            code="OPTIONAL_SCHEMA_INVALID",
                            path=path,
                            criticality="optional",
                            repairability="deterministic",
                            action="quarantine_item",
                            original=original_item,
                            detail="; ".join(item_schema_errors[:10]),
                        )
                    )
                    continue
                identity = json_sha256(normalized_item)
                if identity in seen_optional:
                    repairs.append(
                        _finding(
                            code="DUPLICATE_OPTIONAL_ITEM",
                            path=path,
                            criticality="optional",
                            repairability="deterministic",
                            action="quarantine_duplicate",
                            original=original_item,
                        )
                    )
                    continue
                seen_optional.add(identity)

            if (
                field == "candidates"
                and normalized_item.get("type") == "highlight"
                and item_end - item_start > TEASER_MAXIMUM_SECONDS
            ):
                repairs.append(
                    _finding(
                        code="TEASER_INELIGIBLE_DURATION",
                        path=path,
                        criticality="optional",
                        repairability="derived_eligibility",
                        action="preserve_evidence_exclude_from_teaser",
                        original=original_item,
                        effective=normalized_item,
                        detail=(
                            f"duration={item_end - item_start:.3f}s, "
                            f"maximum={TEASER_MAXIMUM_SECONDS:g}s"
                        ),
                    )
                )

            kept.append(normalized_item)
        effective[field] = kept

    effective_sha256 = json_sha256(effective)
    if errors:
        quality_status = "blocked"
    elif any(
        item.get("action", "").startswith("quarantine")
        or item.get("action") == "replace_with_empty_collection"
        or item.get("code") == "TEASER_INELIGIBLE_DURATION"
        for item in repairs
    ):
        quality_status = "usable_with_warnings"
    elif repairs:
        quality_status = "repaired"
    else:
        quality_status = "valid"
    return WindowAnalysisContractResult(
        effective_window=effective,
        repairs=repairs,
        blockers=blockers,
        errors=errors,
        raw_sha256=raw_sha256,
        effective_sha256=effective_sha256,
        quality_status=quality_status,
    )
