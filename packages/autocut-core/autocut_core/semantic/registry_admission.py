#!/usr/bin/env python3
"""Deterministic partial admission for a model-generated Series Registry."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from autocut_core.io import atomic_write_json, json_sha256, load_json
from autocut_core.schema.compat import validate_task_response
from .registry_contract import (
    RELATIONSHIP_CLOSURE_THRESHOLD,
    RELATIONSHIP_UNCOVERED,
    normalize_character_name,
    validate_series_registry_contract,
)


SCHEMA_VERSION = "1.0"
POLICY_VERSION = "series-registry-partial-admission-v1"
DISTINCT_IDENTITY_QUALIFIER_MARKERS = {
    "ai",
    "a.i.",
    "artificial intelligence",
    "android",
    "clone",
    "computer",
    "digital assistant",
    "robot",
    "system",
    "人工智能",
    "仿生人",
    "克隆体",
    "机器人",
    "系统",
}


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
        return json_sha256(self.raw_registry)

    @property
    def effective_sha256(self) -> str:
        return json_sha256(self.effective_registry)

    @property
    def repairs(self) -> tuple[dict[str, Any], ...]:
        return tuple(
            copy.deepcopy(self.admission.get("local_admission_actions", []))
        )


def _objects(value: Any) -> list[dict[str, Any]]:
    return [item for item in value or [] if isinstance(item, dict)]


def _record_index(values: Any) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in _objects(values)
        if isinstance(item.get("id"), str)
    }


def _qualified_identity_event_ids(
    registry: dict[str, Any],
    *,
    events_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, list[str]], list[dict[str, Any]]]:
    """Find evidence whose role-qualified name is not an admitted alias."""

    by_character: dict[str, list[str]] = {}
    findings: list[dict[str, Any]] = []
    for character in _objects(registry.get("characters")):
        character_id = character.get("id")
        if not isinstance(character_id, str):
            continue
        canonical = normalize_character_name(character.get("canonical_name"))
        if not canonical:
            continue
        admitted_names = {
            normalize_character_name(value)
            for value in [
                character.get("canonical_name"),
                *(character.get("aliases", []) or []),
            ]
            if normalize_character_name(value)
        }
        for event_id in character.get("evidence_event_ids", []) or []:
            event = events_by_id.get(event_id)
            if event is None:
                continue
            names = [
                str(item)
                for item in event.get("character_names", []) or []
                if isinstance(item, str) and item.strip()
            ]
            normalized_names = {
                normalize_character_name(item) for item in names
            }
            if normalized_names & admitted_names:
                continue
            qualified = sorted(
                {
                    name
                    for name in names
                    if (
                        (normalized := normalize_character_name(name))
                        and _has_distinct_identity_qualifier(
                            normalized,
                            canonical,
                        )
                    )
                }
            )
            if not qualified:
                continue
            by_character.setdefault(character_id, []).append(event_id)
            findings.append(
                {
                    "code": "registry_identity_qualifier_conflict",
                    "character_id": character_id,
                    "event_id": event_id,
                    "canonical_name": character.get("canonical_name"),
                    "observed_names": qualified,
                }
            )
    return (
        {
            key: sorted(set(values))
            for key, values in sorted(by_character.items())
        },
        sorted(
            findings,
            key=lambda item: (item["character_id"], item["event_id"]),
        ),
    )


def _has_distinct_identity_qualifier(
    normalized_name: str,
    normalized_canonical: str,
) -> bool:
    qualifier = ""
    for opener, closer in ((" (", ")"), (" [", "]")):
        prefix = normalized_canonical + opener
        if normalized_name.startswith(prefix) and normalized_name.endswith(closer):
            qualifier = normalized_name[len(prefix) : -len(closer)].strip()
            break
    if not qualifier:
        return False
    normalized_qualifier = " ".join(
        qualifier.replace("-", " ").replace("/", " ").split()
    )
    return bool(
        normalized_qualifier in DISTINCT_IDENTITY_QUALIFIER_MARKERS
        or any(
            marker in normalized_qualifier.split()
            for marker in {"ai", "android", "clone", "robot", "system"}
        )
    )


def _review_status(
    character_id: str,
    decisions: Iterable[dict[str, Any]],
) -> str:
    relevant = [
        item
        for item in decisions
        if isinstance(item, dict)
        and item.get("subject_character_id") == character_id
    ]
    if not relevant:
        return "not_required"
    latest = relevant[-1]
    explicit = latest.get("review_status")
    if explicit in {
        "not_required",
        "not_required_weak_evidence",
        "completed",
        "failed",
    }:
        return str(explicit)
    if latest.get("evidence_reviewed") is True:
        return "completed"
    if latest.get("decision") in {
        "repair_review_failed",
        "invalid_repair_review_response",
    }:
        return "failed"
    return "not_required_weak_evidence"


def _filter_event_ids(values: Any, quarantined: set[str]) -> list[str]:
    return [
        item
        for item in values or []
        if isinstance(item, str) and item not in quarantined
    ]


def _expand_quarantine_closure(
    registry: dict[str, Any],
    *,
    event_index: list[dict[str, Any]],
    quarantined_character_ids: set[str],
    quarantined_event_ids: set[str],
) -> tuple[set[str], set[str], dict[str, str]]:
    """Close quarantine over evidence and newly orphaned formal identities."""

    characters = _record_index(registry.get("characters"))
    event_episode = {
        item["id"]: int(item["episode"])
        for item in event_index
        if isinstance(item, dict)
        and isinstance(item.get("id"), str)
        and isinstance(item.get("episode"), int)
    }
    reasons: dict[str, str] = {}
    while True:
        for character_id in sorted(quarantined_character_ids):
            character = characters.get(character_id)
            if character is not None:
                quarantined_event_ids.update(
                    _filter_event_ids(
                        character.get("evidence_event_ids"),
                        set(),
                    )
                )

        active_character_ids: set[str] = set()
        admitted_evidence: dict[str, list[str]] = {}
        newly_quarantined: dict[str, str] = {}
        for character_id, character in characters.items():
            if character_id in quarantined_character_ids:
                continue
            evidence_ids = _filter_event_ids(
                character.get("evidence_event_ids"), quarantined_event_ids
            )
            if not evidence_ids:
                newly_quarantined[character_id] = (
                    "all_identity_evidence_quarantined"
                )
                continue
            active_character_ids.add(character_id)
            admitted_evidence[character_id] = evidence_ids

        related_ids: set[str] = set()
        for relationship in _objects(registry.get("relationships")):
            relationship_character_ids = {
                item
                for item in relationship.get("character_ids", []) or []
                if isinstance(item, str)
            }
            if not relationship_character_ids <= active_character_ids:
                continue
            original_changes = _objects(relationship.get("state_changes"))
            if original_changes and not any(
                item.get("event_id") not in quarantined_event_ids
                for item in original_changes
            ):
                continue
            related_ids.update(relationship_character_ids)

        thread_character_ids: set[str] = set()
        for thread in _objects(registry.get("story_threads")):
            character_ids = {
                item
                for item in thread.get("character_ids", []) or []
                if isinstance(item, str)
            }
            if not character_ids or not character_ids <= active_character_ids:
                continue
            if not _filter_event_ids(
                thread.get("anchor_event_ids"), quarantined_event_ids
            ):
                continue
            thread_character_ids.update(character_ids)

        for character_id in sorted(thread_character_ids - related_ids):
            character = characters[character_id]
            if character.get("entity_type") != "individual":
                continue
            evidence_ids = admitted_evidence.get(character_id, [])
            distinct_episode_count = len(
                {
                    event_episode[event_id]
                    for event_id in evidence_ids
                    if event_id in event_episode
                }
            )
            sustained = (
                distinct_episode_count >= RELATIONSHIP_CLOSURE_THRESHOLD
                if event_episode
                else len(evidence_ids) >= RELATIONSHIP_CLOSURE_THRESHOLD
            )
            if sustained:
                newly_quarantined[character_id] = (
                    "relationship_closure_dependency"
                )

        additions = set(newly_quarantined) - quarantined_character_ids
        if not additions:
            return (
                quarantined_character_ids,
                quarantined_event_ids,
                reasons,
            )
        quarantined_character_ids.update(additions)
        reasons.update(
            {
                character_id: newly_quarantined[character_id]
                for character_id in sorted(additions)
            }
        )


def _collect_string_ids(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for item in value.values():
            found.update(_collect_string_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_collect_string_ids(item))
    elif isinstance(value, str):
        found.add(value)
    return found


def validate_registry_admission_payloads(
    registry: dict[str, Any],
    admission: dict[str, Any],
    quarantine: dict[str, Any],
) -> dict[str, Any]:
    errors: list[str] = []
    if admission.get("schema_version") != SCHEMA_VERSION:
        errors.append("registry admission schema_version is invalid")
    if admission.get("policy_version") != POLICY_VERSION:
        errors.append("registry admission policy_version is invalid")
    if quarantine.get("schema_version") != SCHEMA_VERSION:
        errors.append("registry quarantine schema_version is invalid")
    if quarantine.get("policy_version") != POLICY_VERSION:
        errors.append("registry quarantine policy_version is invalid")
    if admission.get("core_registry_sha256") != json_sha256(registry):
        errors.append("registry admission core_registry_sha256 is stale")
    if admission.get("quarantine_sha256") != json_sha256(quarantine):
        errors.append("registry admission quarantine_sha256 is stale")
    quarantined_ids = {
        item
        for item in quarantine.get("quarantined_ids", []) or []
        if isinstance(item, str)
    }
    leaked_ids = sorted(_collect_string_ids(registry) & quarantined_ids)
    if leaked_ids:
        errors.append(
            "formal Series Registry references quarantined IDs: "
            + ", ".join(leaked_ids)
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "ok": not errors,
        "status": admission.get("status"),
        "core_registry_sha256": json_sha256(registry),
        "admission_sha256": json_sha256(admission),
        "quarantine_sha256": json_sha256(quarantine),
        "quarantined_id_count": len(quarantined_ids),
        "leaked_ids": leaked_ids,
        "errors": errors,
    }


def validate_registry_consumer(
    value: dict[str, Any],
    *,
    quarantine: dict[str, Any],
    where: str,
) -> list[str]:
    quarantined_ids = {
        item
        for item in quarantine.get("quarantined_ids", []) or []
        if isinstance(item, str)
    }
    leaked_ids = sorted(_collect_string_ids(value) & quarantined_ids)
    return (
        [f"{where} references quarantined Registry IDs: {', '.join(leaked_ids)}"]
        if leaked_ids
        else []
    )


def compile_series_registry_admission(
    registry: dict[str, Any],
    *,
    event_index: list[dict[str, Any]],
    relationship_decisions: Iterable[dict[str, Any]] = (),
) -> SeriesRegistryAdmissionResult:
    raw = copy.deepcopy(registry)
    effective = copy.deepcopy(registry)
    events_by_id = _record_index(event_index)
    known_event_ids = set(events_by_id) if event_index else None
    contract = validate_series_registry_contract(
        raw,
        known_event_ids=known_event_ids,
        event_index=event_index,
    )
    global_findings = [
        item for item in contract.findings if item.code != RELATIONSHIP_UNCOVERED
    ]
    relationship_character_ids = {
        character_id
        for item in contract.findings
        if item.code == RELATIONSHIP_UNCOVERED
        for character_id in item.character_ids
    }
    qualified_by_character, identity_findings = _qualified_identity_event_ids(
        raw,
        events_by_id=events_by_id,
    )
    quarantined_event_ids = {
        event_id
        for values in qualified_by_character.values()
        for event_id in values
    }
    quarantined_character_ids = set(relationship_character_ids)
    actions: list[dict[str, Any]] = []
    reasons_by_character: dict[str, list[str]] = {}
    for character_id in sorted(relationship_character_ids):
        reasons_by_character.setdefault(character_id, []).append(
            "relationship_closure_unresolved"
        )
        actions.append(
            {
                "action": "quarantine_relationship_unclosed_character",
                "subject_character_id": character_id,
                "review_status": _review_status(
                    character_id, relationship_decisions
                ),
            }
        )
    for finding in identity_findings:
        character_id = str(finding["character_id"])
        reasons_by_character.setdefault(character_id, []).append(
            "identity_qualifier_conflict"
        )
        actions.append(
            {
                "action": "quarantine_ambiguous_identity_evidence",
                **finding,
            }
        )

    (
        quarantined_character_ids,
        quarantined_event_ids,
        closure_reasons,
    ) = _expand_quarantine_closure(
        raw,
        event_index=event_index,
        quarantined_character_ids=quarantined_character_ids,
        quarantined_event_ids=quarantined_event_ids,
    )
    for character_id, reason in sorted(closure_reasons.items()):
        reasons_by_character.setdefault(character_id, []).append(reason)
        actions.append(
            {
                "action": (
                    "quarantine_character_without_admitted_evidence"
                    if reason == "all_identity_evidence_quarantined"
                    else "quarantine_relationship_dependency"
                ),
                "subject_character_id": character_id,
            }
        )

    raw_characters = _objects(raw.get("characters"))
    projected_characters: list[dict[str, Any]] = []
    for character in raw_characters:
        character_id = character.get("id")
        if not isinstance(character_id, str):
            continue
        if character_id in quarantined_character_ids:
            continue
        projected = copy.deepcopy(character)
        evidence_ids = _filter_event_ids(
            projected.get("evidence_event_ids"), quarantined_event_ids
        )
        if not evidence_ids:
            quarantined_character_ids.add(character_id)
            continue
        projected["evidence_event_ids"] = evidence_ids
        if projected.get("first_event_id") in quarantined_event_ids:
            projected["first_event_id"] = evidence_ids[0]
        projected_characters.append(projected)
    effective["characters"] = projected_characters

    quarantined_relationships: list[dict[str, Any]] = []
    projected_relationships: list[dict[str, Any]] = []
    for relationship in _objects(raw.get("relationships")):
        character_ids = set(relationship.get("character_ids", []) or [])
        projected = copy.deepcopy(relationship)
        original_changes = _objects(projected.get("state_changes"))
        projected["state_changes"] = [
            item
            for item in original_changes
            if item.get("event_id") not in quarantined_event_ids
        ]
        if character_ids & quarantined_character_ids or (
            original_changes and not projected["state_changes"]
        ):
            quarantined_relationships.append(copy.deepcopy(relationship))
        else:
            projected_relationships.append(projected)
    effective["relationships"] = projected_relationships

    quarantined_facts: list[dict[str, Any]] = []
    projected_facts: list[dict[str, Any]] = []
    for fact in _objects(raw.get("facts")):
        projected = copy.deepcopy(fact)
        projected["event_ids"] = _filter_event_ids(
            projected.get("event_ids"), quarantined_event_ids
        )
        if projected["event_ids"]:
            projected_facts.append(projected)
        else:
            quarantined_facts.append(copy.deepcopy(fact))
    effective["facts"] = projected_facts

    quarantined_questions: list[dict[str, Any]] = []
    projected_questions: list[dict[str, Any]] = []
    for question in _objects(raw.get("open_questions")):
        projected = copy.deepcopy(question)
        projected["event_ids"] = _filter_event_ids(
            projected.get("event_ids"), quarantined_event_ids
        )
        if projected["event_ids"]:
            projected_questions.append(projected)
        else:
            quarantined_questions.append(copy.deepcopy(question))
    admitted_question_ids = {
        item["id"]
        for item in projected_questions
        if isinstance(item.get("id"), str)
    }
    effective["open_questions"] = projected_questions

    quarantined_threads: list[dict[str, Any]] = []
    projected_threads: list[dict[str, Any]] = []
    for thread in _objects(raw.get("story_threads")):
        projected = copy.deepcopy(thread)
        projected["anchor_event_ids"] = _filter_event_ids(
            projected.get("anchor_event_ids"), quarantined_event_ids
        )
        projected["open_question_ids"] = [
            item
            for item in projected.get("open_question_ids", []) or []
            if item in admitted_question_ids
        ]
        depends_on_character = bool(
            set(projected.get("character_ids", []) or [])
            & quarantined_character_ids
        )
        if depends_on_character or not projected["anchor_event_ids"]:
            quarantined_threads.append(copy.deepcopy(thread))
        else:
            projected_threads.append(projected)
    effective["story_threads"] = projected_threads

    quarantined_conflicts: list[dict[str, Any]] = []
    projected_conflicts: list[dict[str, Any]] = []
    for conflict in _objects(raw.get("unresolved_identity_conflicts")):
        if set(conflict.get("event_ids", []) or []) & quarantined_event_ids:
            quarantined_conflicts.append(copy.deepcopy(conflict))
        else:
            projected_conflicts.append(copy.deepcopy(conflict))
    effective["unresolved_identity_conflicts"] = projected_conflicts

    quarantined_characters = [
        copy.deepcopy(item)
        for item in raw_characters
        if item.get("id") in quarantined_character_ids
    ]
    quarantine = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "source_registry_sha256": json_sha256(raw),
        "quarantined_character_ids": sorted(quarantined_character_ids),
        "quarantined_event_ids": sorted(quarantined_event_ids),
        "blocked_story_thread_ids": sorted(
            item["id"]
            for item in quarantined_threads
            if isinstance(item.get("id"), str)
        ),
        "character_reasons": [
            {
                "character_id": character_id,
                "reasons": sorted(set(reasons)),
                "review_status": _review_status(
                    character_id, relationship_decisions
                ),
            }
            for character_id, reasons in sorted(reasons_by_character.items())
        ],
        "identity_findings": identity_findings,
        "characters": quarantined_characters,
        "events": [
            copy.deepcopy(events_by_id[event_id])
            for event_id in sorted(quarantined_event_ids)
            if event_id in events_by_id
        ],
        "relationships": quarantined_relationships,
        "facts": quarantined_facts,
        "story_threads": quarantined_threads,
        "open_questions": quarantined_questions,
        "unresolved_identity_conflicts": quarantined_conflicts,
    }
    quarantined_ids = {
        *quarantined_character_ids,
        *quarantined_event_ids,
        *(
            str(item.get("id"))
            for field in (
                "relationships",
                "facts",
                "story_threads",
                "open_questions",
            )
            for item in quarantine[field]
            if isinstance(item.get("id"), str)
        ),
    }
    quarantine["quarantined_ids"] = sorted(quarantined_ids)

    errors = [item.as_error() for item in global_findings]
    schema_errors = validate_task_response("series_registry", effective)
    if schema_errors:
        errors.extend(
            "projected Registry schema error: " + item
            for item in schema_errors[:40]
        )
    core_contract = validate_series_registry_contract(
        effective,
        known_event_ids=known_event_ids,
        event_index=event_index,
    )
    if core_contract.errors:
        errors.extend(
            "projected Registry contract error: " + item
            for item in core_contract.errors
        )
    if not effective.get("story_threads"):
        errors.append("projected Registry has no admitted Story Thread")

    has_quarantine = bool(quarantined_ids or identity_findings)
    status = "blocked" if errors else ("partially_ready" if has_quarantine else "ready")
    character_entries = []
    admitted_character_ids = {
        item.get("id") for item in effective.get("characters", []) or []
    }
    for character in raw_characters:
        character_id = str(character.get("id") or "")
        character_entries.append(
            {
                "character_id": character_id,
                "status": (
                    "admitted"
                    if character_id in admitted_character_ids
                    else "quarantined"
                ),
                "reasons": sorted(
                    set(reasons_by_character.get(character_id, []))
                ),
                "review_status": _review_status(
                    character_id, relationship_decisions
                ),
                "evidence_event_ids": list(
                    character.get("evidence_event_ids", []) or []
                ),
                "quarantined_event_ids": qualified_by_character.get(
                    character_id, []
                ),
            }
        )
    admission = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "status": status,
        "source_registry_sha256": json_sha256(raw),
        "core_registry_sha256": json_sha256(effective),
        "quarantine_sha256": json_sha256(quarantine),
        "counts": {
            "character_count": len(raw_characters),
            "admitted_character_count": len(admitted_character_ids),
            "quarantined_character_count": len(quarantined_character_ids),
            "quarantined_event_count": len(quarantined_event_ids),
            "admitted_story_thread_count": len(projected_threads),
            "blocked_story_thread_count": len(quarantined_threads),
        },
        "characters": character_entries,
        "blocked_story_thread_ids": quarantine["blocked_story_thread_ids"],
        "local_admission_actions": sorted(
            actions,
            key=lambda item: (
                str(item.get("subject_character_id") or item.get("character_id") or ""),
                str(item.get("event_id") or ""),
                str(item.get("action") or ""),
            ),
        ),
        "blocking_errors": sorted(set(errors)),
    }
    validation = validate_registry_admission_payloads(
        effective,
        admission,
        quarantine,
    )
    if errors:
        validation["ok"] = False
        validation["errors"] = list(
            dict.fromkeys([*validation["errors"], *errors])
        )
    return SeriesRegistryAdmissionResult(
        raw_registry=raw,
        effective_registry=effective,
        admission=admission,
        quarantine=quarantine,
        validation=validation,
        errors=tuple(validation["errors"]),
    )


def admission_artifact_paths(output_path: Path) -> dict[str, Path]:
    root = output_path.expanduser().resolve().parent
    return {
        "admission": root / "series-registry-admission.json",
        "quarantine": root / "series-registry-quarantine.json",
        "validation": root / "series-registry-validation.json",
    }


def write_series_registry_admission_artifacts(
    *,
    output_path: Path,
    result: SeriesRegistryAdmissionResult,
    request_signature: str | None = None,
) -> dict[str, Path]:
    paths = admission_artifact_paths(output_path)
    admission = copy.deepcopy(result.admission)
    if request_signature:
        admission["request_signature"] = request_signature
    validation = validate_registry_admission_payloads(
        result.effective_registry,
        admission,
        result.quarantine,
    )
    if result.errors:
        validation["ok"] = False
        validation["errors"] = list(
            dict.fromkeys([*validation["errors"], *result.errors])
        )
    if request_signature:
        validation["request_signature"] = request_signature
    atomic_write_json(paths["quarantine"], result.quarantine, private=True)
    atomic_write_json(paths["admission"], admission)
    atomic_write_json(paths["validation"], validation)
    return paths


def load_and_validate_registry_admission(
    registry_path: Path,
    admission_path: Path | None = None,
    quarantine_path: Path | None = None,
    validation_path: Path | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    registry_path = registry_path.expanduser().resolve()
    paths = admission_artifact_paths(registry_path)
    resolved_admission = (
        admission_path.expanduser().resolve()
        if admission_path is not None
        else paths["admission"]
    )
    resolved_quarantine = (
        quarantine_path.expanduser().resolve()
        if quarantine_path is not None
        else paths["quarantine"]
    )
    resolved_validation = (
        validation_path.expanduser().resolve()
        if validation_path is not None
        else paths["validation"]
    )
    registry = load_json(registry_path)
    admission = load_json(resolved_admission)
    quarantine = load_json(resolved_quarantine)
    persisted_validation = load_json(resolved_validation)
    validation = validate_registry_admission_payloads(
        registry, admission, quarantine
    )
    persisted_errors: list[str] = []
    if persisted_validation.get("schema_version") != SCHEMA_VERSION:
        persisted_errors.append("registry validation schema_version is invalid")
    if persisted_validation.get("policy_version") != POLICY_VERSION:
        persisted_errors.append("registry validation policy_version is invalid")
    if persisted_validation.get("ok") is not True:
        persisted_errors.append("persisted Registry admission validation is not ok")
    for field in (
        "status",
        "core_registry_sha256",
        "admission_sha256",
        "quarantine_sha256",
    ):
        if persisted_validation.get(field) != validation.get(field):
            persisted_errors.append(
                f"persisted Registry validation {field} is stale"
            )
    if (
        not validation["ok"]
        or persisted_errors
        or admission.get("status") == "blocked"
    ):
        raise ValueError(
            "invalid Series Registry admission: "
            + "; ".join(
                [
                    *validation["errors"],
                    *persisted_errors,
                    *(
                        admission.get("blocking_errors", [])
                        if admission.get("status") == "blocked"
                        else []
                    ),
                ]
            )
        )
    return registry, admission, quarantine