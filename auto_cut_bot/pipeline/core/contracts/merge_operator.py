"""merge_operator — Multi-source conflict resolution for subject fields.

Compares LLM-extracted data against API-provided data for each subject,
produces canonical values, provenance records, and conflict records.

Policy (auto_policy):
  - Scalar fields (persona, traits, tone, voice_timbre, visual_features,
    relationship, role): prefer LLM extraction; fall back to API when LLM
    is empty.
  - List fields (personality, aliases): union of both sources.
  - Structural fields (first_episode, last_episode): prefer LLM, keep both
    in provenance.
  - Canonical source is recorded as the supplier of the chosen value.
  - Conflicts are raised when both sources provide non-empty, non-equal
    values for the same scalar field.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


def merge_operator(
    llm_data: dict[str, Any],
    api_data: dict[str, Any],
    *,
    entity_id: str = "",
    entity_table: str = "subjects",
    policy: str = "auto_policy",
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Merge LLM and API data for a single subject entity.

    Args:
        llm_data: Subject data extracted by the LLM (series_registry output).
        api_data: Subject data from the external API / catalogue.
        entity_id: Subject identifier (typically the subject name).
        entity_table: Target DB table name (default ``subjects``).
        policy: Resolution policy name (default ``auto_policy``).

    Returns:
        A 3-tuple of ``(canonical, provenance_records, conflict_records)``.

        ``canonical`` is a dict of field_name -> resolved value suitable for
        writing to the canonical DB row.

        ``provenance_records`` is a list of dicts, each with keys:
        entity_table, entity_id, field_path, values, canonical_source,
        resolved_by.

        ``conflict_records`` is a list of dicts, each with keys:
        entity_table, entity_id, field_path, candidates, severity, status.
    """
    _field_policy = _SCALAR_FIELDS if policy == "auto_policy" else {}

    canonical: dict[str, Any] = {}
    provenance_records: list[dict[str, Any]] = []
    conflict_records: list[dict[str, Any]] = []

    now_iso = datetime.now(timezone.utc).isoformat()

    # ── scalar fields ────────────────────────────────────────────────────
    for field in _SCALAR_FIELDS:
        llm_val = _strip_none(llm_data.get(field))
        api_val = _strip_none(api_data.get(field))

        chosen, chosen_source = _resolve_scalar(llm_val, api_val, field, policy)
        if chosen is not None:
            canonical[field] = chosen

        prov = _build_provenance(
            entity_table=entity_table,
            entity_id=entity_id,
            field_path=field,
            llm_val=llm_val,
            api_val=api_val,
            canonical_source=chosen_source,
            resolved_by=policy,
            resolved_at=now_iso,
        )
        provenance_records.append(prov)

        if _is_conflict(llm_val, api_val):
            conflict = _build_conflict(
                entity_table=entity_table,
                entity_id=entity_id,
                field_path=field,
                llm_val=llm_val,
                api_val=api_val,
                created_at=now_iso,
            )
            conflict_records.append(conflict)

    # ── list / union fields ──────────────────────────────────────────────
    for field in _UNION_FIELDS:
        llm_list = _normalize_list(llm_data.get(field))
        api_list = _normalize_list(api_data.get(field))
        merged = sorted(set(llm_list + api_list))
        canonical[field] = merged

        prov = _build_provenance(
            entity_table=entity_table,
            entity_id=entity_id,
            field_path=field,
            llm_val=llm_list,
            api_val=api_list,
            canonical_source="union" if llm_list and api_list else ("llm" if llm_list else "api"),
            resolved_by=policy,
            resolved_at=now_iso,
        )
        provenance_records.append(prov)

        if set(llm_list) != set(api_list) and llm_list and api_list:
            conflict = _build_conflict(
                entity_table=entity_table,
                entity_id=entity_id,
                field_path=field,
                llm_val=llm_list,
                api_val=api_list,
                severity="low",
                created_at=now_iso,
            )
            conflict_records.append(conflict)

    # ── structural fields (first_episode, last_episode) ──────────────────
    for field in _STRUCTURAL_FIELDS:
        llm_val = llm_data.get(field)
        api_val = api_data.get(field)
        chosen = llm_val if llm_val is not None else api_val
        if chosen is not None:
            canonical[field] = chosen

        prov = _build_provenance(
            entity_table=entity_table,
            entity_id=entity_id,
            field_path=field,
            llm_val=llm_val,
            api_val=api_val,
            canonical_source="llm" if llm_val is not None else "api",
            resolved_by=policy,
            resolved_at=now_iso,
        )
        provenance_records.append(prov)

        if (
            llm_val is not None
            and api_val is not None
            and llm_val != api_val
        ):
            conflict = _build_conflict(
                entity_table=entity_table,
                entity_id=entity_id,
                field_path=field,
                llm_val=llm_val,
                api_val=api_val,
                severity="medium",
                created_at=now_iso,
            )
            conflict_records.append(conflict)

    return canonical, provenance_records, conflict_records


# ── field categorisation ────────────────────────────────────────────────────

_SCALAR_FIELDS: tuple[str, ...] = (
    "persona",
    "traits",
    "tone",
    "voice_timbre",
    "visual_features",
    "relationship",
    "role",
)

_UNION_FIELDS: tuple[str, ...] = (
    "personality",
    "aliases",
)

_STRUCTURAL_FIELDS: tuple[str, ...] = (
    "first_episode",
    "last_episode",
)


# ── internal helpers ────────────────────────────────────────────────────────


def _strip_none(value: Any) -> Any:
    """Return None for empty strings, otherwise the value."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _normalize_list(value: Any) -> list[Any]:
    """Coerce a value to a list; filter out None/empty entries."""
    if value is None:
        return []
    if isinstance(value, list):
        return [v for v in value if v is not None and v != ""]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [v for v in parsed if v is not None and v != ""]
        except (json.JSONDecodeError, TypeError):
            pass
        stripped = value.strip()
        return [stripped] if stripped else []
    return [str(value)] if value else []


def _resolve_scalar(
    llm_val: Any,
    api_val: Any,
    field: str,
    policy: str,
) -> tuple[Any, str]:
    """Resolve a scalar field to a canonical value and source label.

    Policy: prefer LLM extraction; fall back to API when LLM is empty.
    """
    if llm_val is not None:
        return llm_val, "llm"
    if api_val is not None:
        return api_val, "api"
    return None, "none"


def _is_conflict(llm_val: Any, api_val: Any) -> bool:
    """Return True when both sources have non-empty, differing values."""
    llm = _strip_none(llm_val)
    api = _strip_none(api_val)
    if llm is None or api is None:
        return False
    if isinstance(llm, list) and isinstance(api, list):
        return sorted(llm) != sorted(api)
    if isinstance(llm, (int, float)) and isinstance(api, (int, float)):
        return llm != api
    return str(llm).strip() != str(api).strip()


def _build_provenance(
    *,
    entity_table: str,
    entity_id: str,
    field_path: str,
    llm_val: Any,
    api_val: Any,
    canonical_source: str,
    resolved_by: str,
    resolved_at: str,
) -> dict[str, Any]:
    """Build a provenance record dict for insert_provenance."""
    return {
        "entity_table": entity_table,
        "entity_id": entity_id,
        "field_path": field_path,
        "values": json.dumps(
            {"llm": llm_val, "api": api_val}, ensure_ascii=False, default=str
        ),
        "canonical_source": canonical_source,
        "resolved_by": resolved_by,
        "resolved_at": resolved_at,
    }


def _build_conflict(
    *,
    entity_table: str,
    entity_id: str,
    field_path: str,
    llm_val: Any,
    api_val: Any,
    severity: str = "low",
    created_at: str = "",
) -> dict[str, Any]:
    """Build a conflict record dict for upsert_conflicts."""
    return {
        "entity_table": entity_table,
        "entity_id": entity_id,
        "field_path": field_path,
        "candidates": json.dumps(
            {"llm": llm_val, "api": api_val}, ensure_ascii=False, default=str
        ),
        "severity": severity,
        "status": "pending",
        "created_at": created_at,
    }