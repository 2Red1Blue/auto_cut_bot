"""Closed causal-dependency contract for finalized Story Script beats.

The model-facing ``story_script_draft`` intentionally has no causal payload.
``script_preflight`` materializes this payload only after it has resolved the
final beat's evidence references.  This leaf module validates the materialized
object without importing the legacy compatibility-schema module, so the final
Story Script boundary has one explicit owner for its cross-field semantics.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TypeGuard

CAUSAL_DEPENDENCY_KEYS = frozenset(
    {
        "explains_opening_highlight",
        "required_before_fact_ids",
        "required_relationship_ids",
        "required_event_ids",
        "required_thread_beat_ids",
        "causal_ancestor_episode_range",
        "cross_unit_retrieval",
    }
)

DEPENDENCY_ID_KEYS = (
    "required_before_fact_ids",
    "required_relationship_ids",
    "required_event_ids",
    "required_thread_beat_ids",
)

ANCESTOR_RANGE_KEYS = frozenset({"min_episode", "max_episode", "reason"})
CROSS_UNIT_RETRIEVAL_KEYS = frozenset({"required", "source_unit_ids", "retrieval_status"})
CROSS_UNIT_RETRIEVAL_STATUSES = frozenset(
    {"pending", "covered", "partial", "missing", "needs_video_review"}
)


def _is_object_dict(value: object) -> TypeGuard[dict[object, object]]:
    """Narrow an untrusted JSON object without admitting dynamic values."""
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    """Narrow a JSON array while retaining its element trust boundary."""
    return isinstance(value, list)


def _closed_mapping(
    value: object,
    *,
    where: str,
    keys: frozenset[str],
) -> tuple[Mapping[str, object] | None, list[str]]:
    """Validate an exact-key object and retain its safely typed projection."""
    if not _is_object_dict(value):
        return None, [f"{where}: expected object"]
    typed_value: dict[str, object] = {}
    for raw_key, raw_item in value.items():
        if isinstance(raw_key, str):
            typed_value[raw_key] = raw_item
        else:
            return None, [f"{where}: expected string object keys"]

    actual_keys = set(typed_value)
    errors: list[str] = []
    missing = sorted(keys - actual_keys)
    unknown = sorted(actual_keys - keys)
    if missing:
        errors.append(f"{where}: missing required keys {missing}")
    if unknown:
        errors.append(f"{where}: unknown keys {unknown}")
    return typed_value, errors


def _id_list(value: object, *, where: str) -> tuple[list[str] | None, list[str]]:
    """Validate a JSON list of unique, nonblank identifier strings."""
    if not _is_object_list(value):
        return None, [f"{where}: expected array"]

    errors: list[str] = []
    values: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        item_where = f"{where}[{index}]"
        if not isinstance(item, str) or not item.strip():
            errors.append(f"{item_where}: expected nonempty string ID")
            continue
        if item in seen:
            errors.append(f"{item_where}: duplicate ID {item!r}")
            continue
        seen.add(item)
        values.append(item)
    return values, errors


def _validate_ancestor_range(value: object, *, where: str) -> list[str]:
    ancestor_range, errors = _closed_mapping(
        value,
        where=where,
        keys=ANCESTOR_RANGE_KEYS,
    )
    if ancestor_range is None:
        return errors

    low = ancestor_range.get("min_episode")
    high = ancestor_range.get("max_episode")
    reason = ancestor_range.get("reason")
    if not isinstance(low, int) or isinstance(low, bool) or low < 1:
        errors.append(f"{where}.min_episode: expected integer >= 1")
    if not isinstance(high, int) or isinstance(high, bool) or high < 1:
        errors.append(f"{where}.max_episode: expected integer >= 1")
    if (
        isinstance(low, int)
        and not isinstance(low, bool)
        and isinstance(high, int)
        and not isinstance(high, bool)
        and high < low
    ):
        errors.append(f"{where}: max_episode must be >= min_episode")
    if not isinstance(reason, str) or not reason.strip():
        errors.append(f"{where}.reason: expected nonempty string")
    return errors


def _validate_cross_unit_retrieval(
    value: object,
    *,
    where: str,
    explains_opening_highlight: bool | None,
) -> list[str]:
    retrieval, errors = _closed_mapping(
        value,
        where=where,
        keys=CROSS_UNIT_RETRIEVAL_KEYS,
    )
    if retrieval is None:
        return errors

    required = retrieval.get("required")
    if not isinstance(required, bool):
        errors.append(f"{where}.required: expected boolean")

    source_unit_ids, source_unit_errors = _id_list(
        retrieval.get("source_unit_ids"),
        where=f"{where}.source_unit_ids",
    )
    errors.extend(source_unit_errors)

    status = retrieval.get("retrieval_status")
    if not isinstance(status, str) or status not in CROSS_UNIT_RETRIEVAL_STATUSES:
        errors.append(
            f"{where}.retrieval_status: expected one of {sorted(CROSS_UNIT_RETRIEVAL_STATUSES)}"
        )

    if explains_opening_highlight is False:
        if required is True:
            errors.append(
                f"{where}.required: must be false when the beat does not explain "
                "the opening highlight"
            )
        if source_unit_ids:
            errors.append(
                f"{where}.source_unit_ids: must be empty when the beat does not "
                "explain the opening highlight"
            )
        if status != "covered":
            errors.append(
                f"{where}.retrieval_status: must be 'covered' when the beat does "
                "not explain the opening highlight"
            )
    elif required is True and explains_opening_highlight is not True:
        errors.append(f"{where}.required: requires explains_opening_highlight=true")

    return errors


def validate_story_script_causal_dependency(
    value: object,
    *,
    where: str = "causal_dependency",
) -> list[str]:
    """Return deterministic errors for the exact final-beat causal contract.

    ``story_script_draft`` callers must not use this function: causal
    dependency is a preflight-produced final artifact, not a model draft
    field.  A true explanation must name at least one causal prerequisite;
    a false explanation must carry no prerequisite or cross-unit request.
    """
    dependency, errors = _closed_mapping(
        value,
        where=where,
        keys=CAUSAL_DEPENDENCY_KEYS,
    )
    if dependency is None:
        return errors

    explains = dependency.get("explains_opening_highlight")
    if not isinstance(explains, bool):
        errors.append(f"{where}.explains_opening_highlight: expected boolean")
        explanation_value: bool | None = None
    else:
        explanation_value = explains

    dependency_ids: list[str] = []
    for key in DEPENDENCY_ID_KEYS:
        values, id_errors = _id_list(dependency.get(key), where=f"{where}.{key}")
        errors.extend(id_errors)
        if values is not None:
            dependency_ids.extend(values)

    errors.extend(
        _validate_ancestor_range(
            dependency.get("causal_ancestor_episode_range"),
            where=f"{where}.causal_ancestor_episode_range",
        )
    )
    errors.extend(
        _validate_cross_unit_retrieval(
            dependency.get("cross_unit_retrieval"),
            where=f"{where}.cross_unit_retrieval",
            explains_opening_highlight=explanation_value,
        )
    )

    if explanation_value is True and not dependency_ids:
        errors.append(
            f"{where}: explaining the opening highlight requires at least one "
            "causal prerequisite ID"
        )
    if explanation_value is False and dependency_ids:
        errors.append(f"{where}: a non-explanatory beat must not declare causal prerequisite IDs")
    return errors
