#!/usr/bin/env python3
"""Persistent, monotonic recovery state for an invalid Series Registry."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from autocut_core.io import (
    atomic_write_json,
    json_sha256,
    load_json,
    stable_id,
    utc_now,
)
from autocut_core.schema.compat import validate_task_response
from .registry_contract import (
    RELATIONSHIP_UNCOVERED,
    normalize_character_name,
    validate_series_registry_contract,
)


SCHEMA_VERSION = "3.0"
POLICY_VERSION = "series-registry-recovery-v3-monotonic-delta"
MAX_LINEAGE_ITEMS = 64


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
            "policy_version": POLICY_VERSION,
            "incumbent_registry_sha256": json_sha256(
                self.incumbent_registry
            ),
            "incoming_registry_sha256": json_sha256(
                self.incoming_registry
            ),
            "effective_registry_sha256": json_sha256(
                self.effective_registry
            ),
            "uncovered_before": list(self.uncovered_before),
            "uncovered_after": list(self.uncovered_after),
            "imported_relationships": list(self.imported_relationships),
            "skipped_relationships": list(self.skipped_relationships),
            "errors": list(self.errors),
        }


def registry_recovery_candidate_path(
    *,
    output_path: Path,
    signature: str,
) -> Path:
    return (
        output_path.parent
        / ".registry-recovery"
        / f"{signature}.json"
    )


def _relationship_recovery_state(
    registry: dict[str, Any],
) -> tuple[list[str], list[str], list[str]]:
    result = validate_series_registry_contract(registry)
    uncovered: list[str] = []
    other_errors: list[str] = []
    for finding in result.findings:
        if finding.code == RELATIONSHIP_UNCOVERED:
            uncovered.extend(finding.character_ids)
        else:
            other_errors.append(finding.as_error())
    return sorted(set(uncovered)), other_errors, result.errors


def _character_index(
    registry: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in registry.get("characters", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _normalized_names(character: dict[str, Any]) -> set[str]:
    values = [
        character.get("canonical_name"),
        *(character.get("aliases", []) or []),
    ]
    return {
        normalized
        for value in values
        if (normalized := normalize_character_name(value))
    }


def _character_mapping(
    incumbent: dict[str, Any],
    incoming: dict[str, Any],
) -> tuple[dict[str, str], dict[str, str]]:
    incumbent_characters = _character_index(incumbent)
    incoming_characters = _character_index(incoming)
    canonical_owners: dict[str, set[str]] = {}
    for character_id, character in incumbent_characters.items():
        normalized = normalize_character_name(
            character.get("canonical_name")
        )
        if normalized:
            canonical_owners.setdefault(normalized, set()).add(character_id)

    mapping: dict[str, str] = {}
    failures: dict[str, str] = {}
    for incoming_id, incoming_character in incoming_characters.items():
        incoming_name = normalize_character_name(
            incoming_character.get("canonical_name")
        )
        same_id = incumbent_characters.get(incoming_id)
        if same_id is not None:
            if (
                incoming_name
                and incoming_name in _normalized_names(same_id)
                and normalize_character_name(
                    same_id.get("canonical_name")
                ) in _normalized_names(incoming_character)
            ):
                mapping[incoming_id] = incoming_id
            else:
                failures[incoming_id] = (
                    "exact character id has incompatible canonical identity"
                )
            continue
        owners = canonical_owners.get(incoming_name, set())
        if len(owners) == 1:
            mapping[incoming_id] = next(iter(owners))
        elif owners:
            failures[incoming_id] = (
                "canonical name maps to multiple incumbent characters"
            )
        else:
            failures[incoming_id] = (
                "character has no unique incumbent canonical-name match"
            )
    return mapping, failures


def _resolved_event_character_ids(
    event: dict[str, Any],
    *,
    characters: dict[str, dict[str, Any]],
) -> set[str]:
    name_owners: dict[str, set[str]] = {}
    for character_id, character in characters.items():
        for name in _normalized_names(character):
            name_owners.setdefault(name, set()).add(character_id)
    unique_owners = {
        name: next(iter(owners))
        for name, owners in name_owners.items()
        if len(owners) == 1
    }
    return {
        unique_owners[normalized]
        for value in event.get("character_names", []) or []
        if (
            normalized := normalize_character_name(value)
        ) in unique_owners
    }


def _relationship_evidence_error(
    relationship: dict[str, Any],
    *,
    mapped_character_ids: list[str],
    incumbent_characters: dict[str, dict[str, Any]],
    events_by_id: dict[str, dict[str, Any]],
) -> str | None:
    state_changes = [
        item
        for item in relationship.get("state_changes", []) or []
        if isinstance(item, dict)
    ]
    event_ids = [
        item.get("event_id")
        for item in state_changes
        if isinstance(item.get("event_id"), str)
    ]
    if not event_ids:
        return "relationship has no model-authored state-change Event evidence"
    unknown_event_ids = sorted(
        {event_id for event_id in event_ids if event_id not in events_by_id}
    )
    if unknown_event_ids:
        return (
            "relationship references unknown Event IDs: "
            + ", ".join(unknown_event_ids)
        )
    participant_ids = set(mapped_character_ids)
    if len(participant_ids) < 2:
        return "relationship resolves to fewer than two incumbent characters"
    participant_evidence = {
        character_id: {
            event_id
            for event_id in incumbent_characters[character_id].get(
                "evidence_event_ids", []
            )
            or []
            if isinstance(event_id, str)
        }
        for character_id in participant_ids
    }
    supported = False
    for event_id in event_ids:
        evidence_owners = {
            character_id
            for character_id, character_event_ids in participant_evidence.items()
            if event_id in character_event_ids
        }
        resolved_owners = _resolved_event_character_ids(
            events_by_id[event_id],
            characters={
                character_id: incumbent_characters[character_id]
                for character_id in participant_ids
            },
        )
        if len((evidence_owners | resolved_owners) & participant_ids) >= 2:
            supported = True
            break
    if not supported:
        return (
            "relationship Event evidence does not resolve at least two "
            "mapped participants"
        )
    return None


def _relationship_import_id(
    relationship: dict[str, Any],
    *,
    mapped_character_ids: list[str],
    existing_relationships: list[dict[str, Any]],
) -> str:
    incoming_id = relationship.get("id")
    existing_ids = {
        item.get("id")
        for item in existing_relationships
        if isinstance(item.get("id"), str)
    }
    if (
        isinstance(incoming_id, str)
        and incoming_id not in existing_ids
        and mapped_character_ids
        == list(relationship.get("character_ids", []) or [])
    ):
        return incoming_id
    value = {
        "incoming_relationship_sha256": json_sha256(relationship),
        "mapped_character_ids": mapped_character_ids,
    }
    candidate = stable_id("rel-recovery", value)
    if candidate not in existing_ids:
        return candidate
    return stable_id(
        "rel-recovery",
        {**value, "existing_relationship_ids": sorted(existing_ids)},
    )


def merge_registry_relationship_progress(
    incumbent: dict[str, Any],
    incoming: dict[str, Any],
    *,
    event_index: list[dict[str, Any]],
) -> RegistryRecoveryMergeResult:
    """Import only model-authored relationships that shrink incumbent gaps."""

    incumbent_copy = copy.deepcopy(incumbent)
    incoming_copy = copy.deepcopy(incoming)
    effective = copy.deepcopy(incumbent)
    uncovered_before, incumbent_other, _ = _relationship_recovery_state(
        incumbent
    )
    incoming_uncovered, incoming_other, _ = _relationship_recovery_state(
        incoming
    )
    errors: list[str] = []
    skipped: list[dict[str, Any]] = []
    imported: list[dict[str, Any]] = []

    for label, registry in (
        ("incumbent", incumbent),
        ("incoming", incoming),
    ):
        schema_errors = validate_task_response("series_registry", registry)
        if schema_errors:
            errors.extend(
                f"{label} Registry schema error: {item}"
                for item in schema_errors[:20]
            )
    if incumbent_other:
        errors.extend(
            f"incumbent Registry has non-relationship contract error: {item}"
            for item in incumbent_other
        )
    if incoming_other:
        errors.extend(
            f"incoming Registry has non-relationship contract error: {item}"
            for item in incoming_other
        )
    if not uncovered_before:
        return RegistryRecoveryMergeResult(
            incumbent_copy,
            incoming_copy,
            effective,
            (),
            (),
            tuple(errors),
            (),
            (),
        )
    if errors:
        return RegistryRecoveryMergeResult(
            incumbent_copy,
            incoming_copy,
            effective,
            (),
            (),
            tuple(errors),
            tuple(uncovered_before),
            tuple(uncovered_before),
        )

    mapping, mapping_failures = _character_mapping(incumbent, incoming)
    incumbent_characters = _character_index(incumbent)
    events_by_id = {
        item["id"]: item
        for item in event_index
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    remaining_uncovered = set(uncovered_before)
    incoming_relationships = [
        item
        for item in incoming.get("relationships", []) or []
        if isinstance(item, dict)
    ]
    for relationship in incoming_relationships:
        incoming_ids = [
            item
            for item in relationship.get("character_ids", []) or []
            if isinstance(item, str)
        ]
        mapped_ids = [
            mapping[item]
            for item in incoming_ids
            if item in mapping
        ]
        unresolved_ids = [
            item for item in incoming_ids if item not in mapping
        ]
        if unresolved_ids:
            skipped.append(
                {
                    "incoming_relationship_id": relationship.get("id"),
                    "reason": "unsafe_character_mapping",
                    "character_errors": {
                        item: mapping_failures.get(
                            item, "relationship references unknown character"
                        )
                        for item in unresolved_ids
                    },
                }
            )
            continue
        mapped_gap_ids = remaining_uncovered & set(mapped_ids)
        if not mapped_gap_ids:
            continue
        evidence_error = _relationship_evidence_error(
            relationship,
            mapped_character_ids=mapped_ids,
            incumbent_characters=incumbent_characters,
            events_by_id=events_by_id,
        )
        if evidence_error is not None:
            skipped.append(
                {
                    "incoming_relationship_id": relationship.get("id"),
                    "reason": evidence_error,
                }
            )
            continue
        existing_relationships = [
            item
            for item in effective.get("relationships", []) or []
            if isinstance(item, dict)
        ]
        imported_relationship = copy.deepcopy(relationship)
        imported_relationship["character_ids"] = mapped_ids
        imported_relationship["id"] = _relationship_import_id(
            relationship,
            mapped_character_ids=mapped_ids,
            existing_relationships=existing_relationships,
        )
        duplicate = next(
            (
                item
                for item in existing_relationships
                if item.get("character_ids")
                == imported_relationship["character_ids"]
                and item.get("initial_state")
                == imported_relationship.get("initial_state")
                and item.get("state_changes")
                == imported_relationship.get("state_changes")
            ),
            None,
        )
        if duplicate is not None:
            skipped.append(
                {
                    "incoming_relationship_id": relationship.get("id"),
                    "reason": "relationship already exists in incumbent",
                    "existing_relationship_id": duplicate.get("id"),
                }
            )
            continue
        effective.setdefault("relationships", []).append(
            imported_relationship
        )
        imported.append(
            {
                "action": "import_model_authored_relationship_delta",
                "incoming_relationship_id": relationship.get("id"),
                "effective_relationship_id": imported_relationship["id"],
                "incoming_relationship_sha256": json_sha256(relationship),
                "mapped_character_ids": mapped_ids,
                "closed_character_ids": sorted(mapped_gap_ids),
                "evidence_event_ids": [
                    item.get("event_id")
                    for item in relationship.get("state_changes", []) or []
                    if isinstance(item, dict)
                    and isinstance(item.get("event_id"), str)
                ],
            }
        )
        remaining_uncovered -= mapped_gap_ids

    uncovered_after, effective_other, contract_errors = (
        _relationship_recovery_state(effective)
    )
    schema_errors = validate_task_response("series_registry", effective)
    monotonic = set(uncovered_after) <= set(uncovered_before)
    strict_progress = set(uncovered_after) < set(uncovered_before)
    if effective_other:
        errors.extend(
            f"effective Registry has non-relationship contract error: {item}"
            for item in effective_other
        )
    if schema_errors:
        errors.extend(
            f"effective Registry schema error: {item}"
            for item in schema_errors[:20]
        )
    if imported and not monotonic:
        errors.append(
            "relationship delta is non-monotonic: "
            f"before={uncovered_before}, after={uncovered_after}"
        )
    if imported and not strict_progress:
        errors.append(
            "relationship delta did not strictly reduce uncovered characters"
        )
    if errors:
        effective = copy.deepcopy(incumbent)
        imported = []
        uncovered_after = uncovered_before
    elif not imported:
        errors.extend(
            f"relationship delta skipped: {item.get('reason')}"
            for item in skipped
        )
        if not errors and set(incoming_uncovered) != set(uncovered_before):
            errors.append(
                "incoming Registry changed uncovered character identities "
                "without a safely importable relationship delta"
            )
    elif contract_errors and set(uncovered_after) == set(uncovered_before):
        errors.extend(contract_errors)

    return RegistryRecoveryMergeResult(
        incumbent_copy,
        incoming_copy,
        effective,
        tuple(imported),
        tuple(skipped),
        tuple(errors),
        tuple(uncovered_before),
        tuple(uncovered_after),
    )


def load_registry_recovery_state(
    *,
    output_path: Path,
    signature: str,
) -> dict[str, Any] | None:
    path = registry_recovery_candidate_path(
        output_path=output_path,
        signature=signature,
    )
    if not path.is_file():
        return None
    try:
        payload = load_json(path)
    except Exception:
        return None
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != SCHEMA_VERSION
        or payload.get("policy_version") != POLICY_VERSION
        or payload.get("request_signature") != signature
        or not isinstance(payload.get("effective_registry"), dict)
    ):
        return None
    return payload


def load_registry_recovery_candidate(
    *,
    output_path: Path,
    signature: str,
) -> dict[str, Any] | None:
    state = load_registry_recovery_state(
        output_path=output_path,
        signature=signature,
    )
    if state is None:
        return None
    return copy.deepcopy(state["effective_registry"])


def persist_registry_recovery_candidate(
    registry: dict[str, Any],
    *,
    output_path: Path,
    signature: str,
    errors: list[str] | tuple[str, ...],
    event_index: list[dict[str, Any]] | None = None,
    source: str = "model_response",
    repairs: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    decisions: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
) -> Path | None:
    """Persist only evidence-valid, relationship-only candidates."""

    uncovered, other_errors, contract_errors = _relationship_recovery_state(
        registry
    )
    if not uncovered or other_errors:
        return None
    path = registry_recovery_candidate_path(
        output_path=output_path,
        signature=signature,
    )
    existing = load_registry_recovery_state(
        output_path=output_path,
        signature=signature,
    )
    merge_audit: dict[str, Any] | None = None
    if existing is not None:
        existing_registry = existing["effective_registry"]
        merge_result = merge_registry_relationship_progress(
            existing_registry,
            registry,
            event_index=list(event_index or []),
        )
        merge_audit = merge_result.as_audit()
        if not merge_result.progressed:
            return path
        registry = merge_result.effective_registry
        uncovered, other_errors, contract_errors = (
            _relationship_recovery_state(registry)
        )
        if not uncovered or other_errors:
            return None
        base_registry = existing.get("base_registry", existing_registry)
        lineage = list(existing.get("recovery_lineage", []) or [])
    else:
        base_registry = registry
        lineage = []

    candidate_sha256 = json_sha256(registry)
    lineage.append(
        {
            "recorded_at": utc_now(),
            "source": source,
            "effective_registry_sha256": candidate_sha256,
            "uncovered_character_ids": uncovered,
            "reported_errors": list(errors)[:20],
            "contract_errors": contract_errors[:20],
            "repair_count": len(repairs),
            "decision_count": len(decisions),
            "relationship_delta_merge": merge_audit,
        }
    )
    lineage = lineage[-MAX_LINEAGE_ITEMS:]
    atomic_write_json(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "request_signature": signature,
            "saved_at": utc_now(),
            "status": "partial",
            "base_registry_sha256": json_sha256(base_registry),
            "effective_registry_sha256": candidate_sha256,
            "uncovered_character_ids": uncovered,
            "residual_errors": contract_errors[:20],
            "base_registry": copy.deepcopy(base_registry),
            "effective_registry": copy.deepcopy(registry),
            "applied_repairs": copy.deepcopy(list(repairs)),
            "decisions": copy.deepcopy(list(decisions)),
            "recovery_lineage": lineage,
        },
        private=True,
    )
    return path