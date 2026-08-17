#!/usr/bin/env python3
"""Evidence-gated identity repair for relationship-closure dead ends."""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Callable

from autocut_core.io import json_sha256
from .registry_contract import (
    normalize_character_name,
    validate_series_registry_contract,
)


POLICY_VERSION = "series-registry-identity-repair-v1"
STAGE_VERSION = "story-first-series-registry-identity-audit-v1"
MAX_CANDIDATES = 12

IdentityResolver = Callable[[str, dict[str, Any], dict[str, Any]], dict[str, Any]]

_GENERIC_NAME_PATTERNS = (
    re.compile(r"^(?:the\s+)?(?:rival\s+)?(?:young\s+|old\s+|dark-haired\s+|red-haired\s+|blonde\s+)?(?:woman|man|girl|boy|lady|noblewoman)$", re.I),
    re.compile(r"^(?:old\s+hag|photographer|intruder|attacker|customer)$", re.I),
    re.compile(r"^(?:女子|女人|男人|男子|女孩|男孩|老妪|老妇|对手|摄影师|入侵者|袭击者)$"),
)


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
        return json_sha256(self.raw_registry)

    @property
    def effective_sha256(self) -> str:
        return json_sha256(self.effective_registry)

    def as_report(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "policy_version": POLICY_VERSION,
            "status": "repaired" if self.ok else "blocked",
            "raw_registry_sha256": self.raw_sha256,
            "effective_registry_sha256": self.effective_sha256,
            "repair_count": len(self.repairs),
            "repairs": list(self.repairs),
            "decisions": list(self.decisions),
            "blocking_errors": list(self.errors),
        }


def is_generic_character_name(value: Any) -> bool:
    normalized = " ".join(str(value or "").strip().split())
    return bool(normalized) and any(
        pattern.fullmatch(normalized) for pattern in _GENERIC_NAME_PATTERNS
    )


def _character_index(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in registry.get("characters", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _event_index(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in events
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _identity_terms(character: dict[str, Any]) -> set[str]:
    text = " ".join(
        [
            str(character.get("canonical_name") or ""),
            str(character.get("identity") or ""),
            *[str(item) for item in character.get("goals", []) or []],
        ]
    ).casefold()
    return {
        token
        for token in re.findall(r"[a-z0-9]{4,}|[一-鿿]{2,}", text)
        if token not in {"character", "woman", "person", "individual"}
    }


def build_identity_audit_context(
    registry: dict[str, Any],
    *,
    subject_character_id: str,
    event_index: list[dict[str, Any]],
) -> dict[str, Any]:
    characters = _character_index(registry)
    subject = characters[subject_character_id]
    events_by_id = _event_index(event_index)
    subject_event_ids = [
        item
        for item in subject.get("evidence_event_ids", []) or []
        if isinstance(item, str) and item in events_by_id
    ]
    subject_threads = [
        item
        for item in registry.get("story_threads", []) or []
        if isinstance(item, dict)
        and subject_character_id in (item.get("character_ids", []) or [])
    ]
    subject_thread_ids = {
        item.get("id") for item in subject_threads if isinstance(item.get("id"), str)
    }
    subject_terms = _identity_terms(subject)
    candidates: list[tuple[int, dict[str, Any]]] = []
    for character_id, character in characters.items():
        if character_id == subject_character_id:
            continue
        candidate_thread_ids = {
            item.get("id")
            for item in registry.get("story_threads", []) or []
            if isinstance(item, dict)
            and character_id in (item.get("character_ids", []) or [])
            and isinstance(item.get("id"), str)
        }
        score = 5 * len(subject_thread_ids & candidate_thread_ids)
        score += len(subject_terms & _identity_terms(character))
        canonical = str(character.get("canonical_name") or "").casefold()
        score += sum(
            3
            for event_id in subject_event_ids
            if canonical
            and canonical in str(events_by_id[event_id].get("summary") or "").casefold()
        )
        candidates.append(
            (
                score,
                {
                    key: copy.deepcopy(character.get(key))
                    for key in (
                        "id",
                        "canonical_name",
                        "aliases",
                        "entity_type",
                        "identity",
                        "identity_evidence",
                        "goals",
                        "first_event_id",
                        "evidence_event_ids",
                    )
                },
            )
        )
    candidates.sort(
        key=lambda item: (-item[0], str(item[1].get("id") or ""))
    )
    return {
        "schema_version": "1.0",
        "language": registry.get("language"),
        "subject_character": copy.deepcopy(subject),
        "candidate_characters": [item[1] for item in candidates[:MAX_CANDIDATES]],
        "subject_events": [copy.deepcopy(events_by_id[item]) for item in subject_event_ids],
        "subject_story_threads": copy.deepcopy(subject_threads),
        "existing_relationships": [
            copy.deepcopy(item)
            for item in registry.get("relationships", []) or []
            if isinstance(item, dict)
        ],
        "audit_contract": {
            "subject_character_id": subject_character_id,
            "merge_requires_same_person_evidence": True,
            "appearance_only_match_is_forbidden": True,
            "direct_canonical_cooccurrence_forbids_merge": True,
            "quarantine_requires_redundant_event_coverage": True,
        },
    }


def build_identity_audit_schema(context: dict[str, Any]) -> dict[str, Any]:
    subject_id = context["audit_contract"]["subject_character_id"]
    target_ids = [
        item["id"]
        for item in context.get("candidate_characters", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    event_ids = [
        item["id"]
        for item in context.get("subject_events", [])
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    ]
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "subject_character_id": {"type": "string", "const": subject_id},
            "decision": {
                "type": "string",
                "enum": [
                    "merge_with_existing_character",
                    "quarantine_unresolved_identity",
                    "keep_blocking",
                ],
            },
            "target_character_id": {"type": "string", "enum": ["", *target_ids]},
            "evidence_event_ids": {
                "type": "array",
                "items": {"type": "string", "enum": event_ids or [""]},
                "uniqueItems": True,
            },
            "reason": {"type": "string", "minLength": 1},
        },
        "required": [
            "schema_version",
            "subject_character_id",
            "decision",
            "target_character_id",
            "evidence_event_ids",
            "reason",
        ],
        "additionalProperties": False,
    }


def validate_identity_audit_response(
    response: dict[str, Any],
    *,
    context: dict[str, Any],
) -> list[str]:
    errors: list[str] = []
    subject_id = context["audit_contract"]["subject_character_id"]
    target_ids = {
        item.get("id")
        for item in context.get("candidate_characters", [])
        if isinstance(item, dict)
    }
    allowed_event_ids = {
        item.get("id")
        for item in context.get("subject_events", [])
        if isinstance(item, dict)
    }
    decision = response.get("decision")
    target_id = response.get("target_character_id")
    evidence_event_ids = response.get("evidence_event_ids")
    if response.get("schema_version") != "1.0":
        errors.append("identity audit schema_version must be 1.0")
    if response.get("subject_character_id") != subject_id:
        errors.append("identity audit subject_character_id does not match context")
    if decision not in {
        "merge_with_existing_character",
        "quarantine_unresolved_identity",
        "keep_blocking",
    }:
        errors.append("identity audit decision is invalid")
    if not isinstance(evidence_event_ids, list):
        errors.append("identity audit evidence_event_ids must be an array")
        evidence_event_ids = []
    unknown_events = sorted(
        {
            item
            for item in evidence_event_ids
            if not isinstance(item, str) or item not in allowed_event_ids
        },
        key=str,
    )
    if unknown_events:
        errors.append(f"identity audit contains unknown evidence Event IDs: {unknown_events}")
    if decision == "merge_with_existing_character":
        if target_id not in target_ids:
            errors.append("identity merge target is not a listed candidate")
        if not evidence_event_ids:
            errors.append("identity merge requires Event evidence")
    elif target_id not in {"", None}:
        errors.append("non-merge identity decision requires empty target_character_id")
    if not isinstance(response.get("reason"), str) or not response["reason"].strip():
        errors.append("identity audit reason is required")
    return errors


def _direct_canonical_cooccurrence(
    subject: dict[str, Any],
    target: dict[str, Any],
    *,
    events: list[dict[str, Any]],
) -> list[str]:
    subject_name = normalize_character_name(subject.get("canonical_name"))
    target_name = normalize_character_name(target.get("canonical_name"))
    if not subject_name or not target_name:
        return []
    conflicts: list[str] = []
    for event in events:
        names = {
            normalize_character_name(item)
            for item in event.get("character_names", []) or []
        }
        if subject_name in names and target_name in names:
            event_id = event.get("id")
            if isinstance(event_id, str):
                conflicts.append(event_id)
    return sorted(set(conflicts))


def _replace_thread_character(
    registry: dict[str, Any],
    *,
    subject_id: str,
    target_id: str | None,
) -> None:
    for thread in registry.get("story_threads", []) or []:
        if not isinstance(thread, dict):
            continue
        rewritten: list[Any] = []
        for character_id in thread.get("character_ids", []) or []:
            replacement = target_id if character_id == subject_id else character_id
            if replacement is not None and replacement not in rewritten:
                rewritten.append(replacement)
        thread["character_ids"] = rewritten


def _apply_merge(
    registry: dict[str, Any],
    *,
    subject_id: str,
    target_id: str,
    events: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    effective = copy.deepcopy(registry)
    characters = _character_index(effective)
    subject = characters.get(subject_id)
    target = characters.get(target_id)
    if subject is None or target is None:
        return effective, None, "identity merge subject or target is missing"
    if subject.get("entity_type") != "individual" or target.get("entity_type") != "individual":
        return effective, None, "identity merge requires two individual characters"
    if any(
        subject_id in (item.get("character_ids", []) or [])
        for item in effective.get("relationships", []) or []
        if isinstance(item, dict)
    ):
        return effective, None, "identity merge subject already owns a relationship"
    conflicts = _direct_canonical_cooccurrence(subject, target, events=events)
    if conflicts:
        return effective, None, (
            "identity merge forbidden by direct canonical cooccurrence: "
            + ", ".join(conflicts)
        )
    target_event_ids = [
        item for item in target.get("evidence_event_ids", []) or [] if isinstance(item, str)
    ]
    imported_event_ids = [
        item
        for item in subject.get("evidence_event_ids", []) or []
        if isinstance(item, str) and item not in target_event_ids
    ]
    target["evidence_event_ids"] = [*target_event_ids, *imported_event_ids]
    events_by_id = _event_index(events)
    first_candidates = [target.get("first_event_id"), subject.get("first_event_id")]
    valid_first = [item for item in first_candidates if isinstance(item, str)]
    if valid_first:
        target["first_event_id"] = min(
            valid_first,
            key=lambda item: (
                int(events_by_id.get(item, {}).get("episode") or 10**9),
                item,
            ),
        )
    effective["characters"] = [
        item
        for item in effective.get("characters", []) or []
        if not (isinstance(item, dict) and item.get("id") == subject_id)
    ]
    _replace_thread_character(effective, subject_id=subject_id, target_id=target_id)
    return (
        effective,
        {
            "action": "merge_duplicate_character_identity",
            "subject_character_id": subject_id,
            "target_character_id": target_id,
            "imported_evidence_event_ids": imported_event_ids,
            "discarded_subject_labels": [
                subject.get("canonical_name"),
                *(subject.get("aliases", []) or []),
            ],
        },
        None,
    )


def _apply_quarantine(
    registry: dict[str, Any],
    *,
    subject_id: str,
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    effective = copy.deepcopy(registry)
    characters = _character_index(effective)
    subject = characters.get(subject_id)
    if subject is None:
        return effective, None, "identity quarantine subject is missing"
    if not is_generic_character_name(subject.get("canonical_name")):
        return effective, None, "identity quarantine requires a generic role label"
    if any(
        subject_id in (item.get("character_ids", []) or [])
        for item in effective.get("relationships", []) or []
        if isinstance(item, dict)
    ):
        return effective, None, "identity quarantine subject owns a relationship"
    subject_event_ids = {
        item
        for item in subject.get("evidence_event_ids", []) or []
        if isinstance(item, str)
    }
    covered_by_others = {
        event_id
        for character_id, character in characters.items()
        if character_id != subject_id
        for event_id in character.get("evidence_event_ids", []) or []
        if isinstance(event_id, str)
    }
    unique_event_ids = sorted(subject_event_ids - covered_by_others)
    if unique_event_ids:
        return effective, None, (
            "identity quarantine would discard uniquely owned Event evidence: "
            + ", ".join(unique_event_ids)
        )
    for thread in effective.get("story_threads", []) or []:
        if not isinstance(thread, dict) or subject_id not in (thread.get("character_ids", []) or []):
            continue
        survivors = [
            item for item in thread.get("character_ids", []) or [] if item != subject_id
        ]
        if not survivors:
            return effective, None, (
                f"identity quarantine would orphan story thread {thread.get('id')}"
            )
    effective["characters"] = [
        item
        for item in effective.get("characters", []) or []
        if not (isinstance(item, dict) and item.get("id") == subject_id)
    ]
    _replace_thread_character(effective, subject_id=subject_id, target_id=None)
    conflict = {
        "description": (
            f"Quarantined unresolved generic identity {subject.get('canonical_name')!r}; "
            "all explicit Event evidence is covered by retained characters."
        ),
        "candidate_names": [
            str(item)
            for item in [subject.get("canonical_name"), *(subject.get("aliases", []) or [])]
            if str(item or "").strip()
        ],
        "event_ids": sorted(subject_event_ids),
    }
    effective.setdefault("unresolved_identity_conflicts", []).append(conflict)
    return (
        effective,
        {
            "action": "quarantine_redundant_generic_character",
            "subject_character_id": subject_id,
            "evidence_event_ids": sorted(subject_event_ids),
        },
        None,
    )


def repair_series_registry_identities(
    registry: dict[str, Any],
    *,
    event_index: list[dict[str, Any]],
    relationship_decisions: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    resolver: IdentityResolver,
) -> SeriesRegistryIdentityRepairResult:
    """Audit reviewed relationship dead ends and apply only gated repairs."""

    raw = copy.deepcopy(registry)
    effective = copy.deepcopy(registry)
    repairs: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    errors: list[str] = []
    subject_ids = sorted(
        {
            str(item.get("subject_character_id"))
            for item in relationship_decisions
            if isinstance(item, dict)
            and item.get("decision") == "no_supported_relationship"
            and (
                item.get("review_status")
                in {"completed", "not_required_weak_evidence"}
                or item.get("evidence_reviewed") is True
            )
            and isinstance(item.get("subject_character_id"), str)
        }
    )
    if not subject_ids:
        return SeriesRegistryIdentityRepairResult(
            raw,
            effective,
            (),
            (),
            (
                "identity repair requires a completed or explicitly "
                "not-required relationship review",
            ),
        )

    for subject_id in subject_ids:
        if subject_id not in _character_index(effective):
            continue
        context = build_identity_audit_context(
            effective,
            subject_character_id=subject_id,
            event_index=event_index,
        )
        schema = build_identity_audit_schema(context)
        job_id = f"registry-identity-{subject_id}"
        response = resolver(job_id, context, schema)
        response_errors = validate_identity_audit_response(response, context=context)
        if response_errors:
            decisions.append(
                {
                    "job_id": job_id,
                    "subject_character_id": subject_id,
                    "decision": "invalid_identity_audit",
                    "errors": response_errors,
                    "response_sha256": json_sha256(response),
                }
            )
            errors.extend(response_errors)
            continue
        decision = str(response["decision"])
        decision_record = {
            "job_id": job_id,
            "subject_character_id": subject_id,
            "decision": decision,
            "target_character_id": response.get("target_character_id") or "",
            "evidence_event_ids": list(response.get("evidence_event_ids") or []),
            "reason": response.get("reason"),
            "response_sha256": json_sha256(response),
        }
        decisions.append(decision_record)
        if decision == "keep_blocking":
            errors.append(
                f"identity audit kept {subject_id} blocking: {response.get('reason')}"
            )
            continue
        if decision == "merge_with_existing_character":
            candidate, repair, repair_error = _apply_merge(
                effective,
                subject_id=subject_id,
                target_id=str(response["target_character_id"]),
                events=event_index,
            )
        else:
            candidate, repair, repair_error = _apply_quarantine(
                effective,
                subject_id=subject_id,
            )
        if repair_error is not None or repair is None:
            errors.append(repair_error or "identity repair produced no change")
            continue
        effective = candidate
        repair["job_id"] = job_id
        repair["response_sha256"] = json_sha256(response)
        repairs.append(repair)

    known_event_ids = set(_event_index(event_index))
    contract_errors = validate_series_registry_contract(
        effective,
        known_event_ids=known_event_ids,
        event_index=event_index,
    ).errors
    if contract_errors:
        errors.extend(contract_errors)
    if errors:
        effective = copy.deepcopy(raw)
        repairs = []
    return SeriesRegistryIdentityRepairResult(
        raw_registry=raw,
        effective_registry=effective,
        repairs=tuple(repairs),
        decisions=tuple(decisions),
        errors=tuple(dict.fromkeys(errors)),
    )