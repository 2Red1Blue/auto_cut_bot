#!/usr/bin/env python3
"""Evidence-locked semantic repair for Series Registry relationship closure."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any, Callable

from autocut_core.io import json_sha256, stable_id
from autocut_core.schema.compat import validate_schema
from .registry_contract import (
    RELATIONSHIP_UNCOVERED,
    normalize_character_name,
    validate_series_registry_contract,
)


POLICY_VERSION = "series-registry-relationship-semantic-repair-v3"
STAGE_VERSION = (
    "story-first-series-registry-relationship-repair-v3-direct-review"
)
MAX_REPAIR_JOBS = 8
MAX_PARTNER_CANDIDATES = 8
MAX_SHARED_EVENTS_PER_PARTNER = 8


RepairResolver = Callable[
    [str, dict[str, Any], dict[str, Any]],
    dict[str, Any],
]


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


def _relationship_findings(registry: dict[str, Any]) -> tuple[list[str], list[str]]:
    result = validate_series_registry_contract(registry)
    uncovered: list[str] = []
    other_errors: list[str] = []
    for finding in result.findings:
        if finding.code == RELATIONSHIP_UNCOVERED:
            uncovered.extend(finding.character_ids)
        else:
            other_errors.append(finding.as_error())
    return sorted(set(uncovered)), other_errors


def is_relationship_closure_only(registry: dict[str, Any]) -> bool:
    uncovered, other_errors = _relationship_findings(registry)
    return bool(uncovered) and not other_errors


def _event_index_by_id(
    event_index: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        item["id"]: item
        for item in event_index
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _unique_character_name_owners(
    characters: dict[str, dict[str, Any]],
) -> dict[str, str]:
    owners: dict[str, set[str]] = {}
    for character_id, character in characters.items():
        canonical_variants = _canonical_name_variants(
            character.get("canonical_name")
        )
        alias_variants = {
            normalize_character_name(value)
            for value in character.get("aliases", []) or []
            if normalize_character_name(value)
        }
        for normalized in canonical_variants | alias_variants:
            if normalized:
                owners.setdefault(normalized, set()).add(character_id)
    return {
        normalized: next(iter(character_ids))
        for normalized, character_ids in owners.items()
        if len(character_ids) == 1
    }


def _canonical_name_variants(value: Any) -> set[str]:
    normalized = normalize_character_name(value)
    if not normalized:
        return set()
    variants = {normalized}
    tokens = [
        token
        for token in normalized.replace("-", " ").split()
        if len(token) >= 4 and token.isalnum()
    ]
    variants.update(tokens)
    return variants


def _resolved_event_character_ids(
    event: dict[str, Any],
    *,
    name_owners: dict[str, str],
) -> set[str]:
    return {
        name_owners[normalized]
        for value in event.get("character_names", []) or []
        if (normalized := normalize_character_name(value)) in name_owners
    }


def _text_mentions_name(text: Any, name: Any) -> bool:
    normalized_text = normalize_character_name(text)
    normalized_name = normalize_character_name(name)
    if not normalized_text or not normalized_name:
        return False
    if all(
        char.isascii() and (char.isalnum() or char.isspace())
        for char in normalized_name
    ):
        padded_text = f" {normalized_text} "
        return f" {normalized_name} " in padded_text
    return normalized_name in normalized_text


def _supporting_facts_for_pair(
    registry: dict[str, Any],
    *,
    subject: dict[str, Any],
    partner: dict[str, Any],
    events_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    subject_names = _canonical_name_variants(
        subject.get("canonical_name")
    )
    partner_names = _canonical_name_variants(
        partner.get("canonical_name")
    )
    supported: list[dict[str, Any]] = []
    for fact in registry.get("facts", []) or []:
        if not isinstance(fact, dict):
            continue
        statement = fact.get("statement")
        if not (
            any(
                _text_mentions_name(statement, name)
                for name in subject_names
            )
            and any(
                _text_mentions_name(statement, name)
                for name in partner_names
            )
        ):
            continue
        event_ids = [
            event_id
            for event_id in fact.get("event_ids", []) or []
            if isinstance(event_id, str) and event_id in events_by_id
        ]
        if not event_ids:
            continue
        supported.append(
            {
                "id": fact.get("id"),
                "statement": statement,
                "event_ids": event_ids,
            }
        )
    return supported


def build_relationship_repair_context(
    registry: dict[str, Any],
    *,
    event_index: list[dict[str, Any]],
    subject_character_id: str,
) -> dict[str, Any]:
    characters = {
        item["id"]: item
        for item in registry.get("characters", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    if subject_character_id not in characters:
        raise ValueError(
            f"relationship repair subject is unknown: {subject_character_id}"
        )
    subject = characters[subject_character_id]
    events_by_id = _event_index_by_id(event_index)
    name_owners = _unique_character_name_owners(characters)
    resolved_event_characters = {
        event_id: _resolved_event_character_ids(
            event,
            name_owners=name_owners,
        )
        for event_id, event in events_by_id.items()
    }
    subject_events = {
        value
        for value in subject.get("evidence_event_ids", []) or []
        if isinstance(value, str) and value in events_by_id
    }
    partner_candidates: list[dict[str, Any]] = []
    fact_details: dict[str, dict[str, Any]] = {}
    fact_partners: dict[str, set[str]] = {}
    event_sources: dict[str, set[str]] = {}
    for partner_id, partner in characters.items():
        if partner_id == subject_character_id:
            continue
        partner_events = {
            value
            for value in partner.get("evidence_event_ids", []) or []
            if isinstance(value, str) and value in events_by_id
        }
        direct_intersection = subject_events & partner_events
        cooccurrence_events = {
            event_id
            for event_id, resolved_ids in resolved_event_characters.items()
            if (
                {subject_character_id, partner_id} <= resolved_ids
                or (
                    event_id in subject_events
                    and partner_id in resolved_ids
                )
                or (
                    event_id in partner_events
                    and subject_character_id in resolved_ids
                )
            )
        }
        direct_resolved_cooccurrence_events = {
            event_id
            for event_id, resolved_ids in resolved_event_characters.items()
            if {subject_character_id, partner_id} <= resolved_ids
        }
        supporting_facts = _supporting_facts_for_pair(
            registry,
            subject=subject,
            partner=partner,
            events_by_id=events_by_id,
        )
        fact_event_ids = {
            event_id
            for fact in supporting_facts
            for event_id in fact["event_ids"]
        }
        all_shared = (
            direct_intersection | cooccurrence_events | fact_event_ids
        )
        if not all_shared:
            continue
        shared = []
        for evidence_group in (
            fact_event_ids,
            cooccurrence_events,
            direct_intersection,
        ):
            for event_id in sorted(evidence_group):
                if event_id not in shared:
                    shared.append(event_id)
        selected_shared = shared[:MAX_SHARED_EVENTS_PER_PARTNER]
        for event_id in direct_intersection:
            event_sources.setdefault(event_id, set()).add(
                "character_evidence_intersection"
            )
        for event_id in cooccurrence_events:
            event_sources.setdefault(event_id, set()).add(
                "event_character_cooccurrence"
            )
        for event_id in fact_event_ids:
            event_sources.setdefault(event_id, set()).add(
                "registry_fact"
            )
        for fact in supporting_facts:
            fact_id = str(fact.get("id") or "")
            if not fact_id:
                continue
            fact_details[fact_id] = fact
            fact_partners.setdefault(fact_id, set()).add(partner_id)
        strong_reasons: list[str] = []
        if supporting_facts:
            strong_reasons.append("registry_fact_names_both_characters")
        if direct_resolved_cooccurrence_events:
            strong_reasons.append(
                "direct_resolved_character_cooccurrence"
            )
        if len(cooccurrence_events) >= 2:
            strong_reasons.append("multiple_resolved_character_cooccurrences")
        if len(all_shared) >= 3:
            strong_reasons.append("three_or_more_supported_events")
        partner_candidates.append(
            {
                "character_id": partner_id,
                "canonical_name": partner.get("canonical_name"),
                "entity_type": partner.get("entity_type"),
                "shared_event_ids": selected_shared,
                "shared_event_count": len(all_shared),
                "supporting_fact_ids": sorted(
                    str(item.get("id"))
                    for item in supporting_facts
                    if item.get("id")
                ),
                "strong_evidence": bool(strong_reasons),
                "strong_evidence_reasons": strong_reasons,
            }
        )
    partner_candidates.sort(
        key=lambda item: (
            -int(bool(item.get("strong_evidence"))),
            -int(item["shared_event_count"]),
            str(item["character_id"]),
        )
    )
    partner_candidates = partner_candidates[:MAX_PARTNER_CANDIDATES]
    allowed_event_ids = {
        event_id
        for item in partner_candidates
        for event_id in item["shared_event_ids"]
    }
    compact_events = []
    for event_id in sorted(allowed_event_ids):
        event = events_by_id.get(event_id)
        if event is None:
            continue
        compact_events.append(
            {
                key: event.get(key)
                for key in (
                    "id",
                    "episode",
                    "summary",
                    "function",
                    "character_names",
                    "cause",
                    "effect",
                )
            }
        )
        compact_events[-1]["evidence_sources"] = sorted(
            event_sources.get(event_id, set())
        )
    partner_ids = {
        item["character_id"]
        for item in partner_candidates
    }
    supporting_fact_payload = []
    for fact_id in sorted(fact_details):
        partners = sorted(fact_partners.get(fact_id, set()) & partner_ids)
        if not partners:
            continue
        selected_fact_event_ids = [
            event_id
            for event_id in fact_details[fact_id].get("event_ids", []) or []
            if event_id in allowed_event_ids
        ]
        if not selected_fact_event_ids:
            continue
        supporting_fact_payload.append(
            {
                **fact_details[fact_id],
                "event_ids": selected_fact_event_ids,
                "partner_character_ids": partners,
            }
        )
    return {
        "schema_version": "1.0",
        "language": registry.get("language"),
        "subject_character": subject,
        "partner_candidates": partner_candidates,
        "shared_events": compact_events,
        "supporting_facts": supporting_fact_payload,
        "existing_relationships": [
            item
            for item in registry.get("relationships", []) or []
            if isinstance(item, dict)
            and set(item.get("character_ids", []) or [])
            & ({subject_character_id} | set(partner_ids))
        ],
        "repair_contract": {
            "subject_character_id": subject_character_id,
            "registry_is_frozen": True,
            "add_relationship_only": True,
            "partner_must_be_listed": True,
            "state_change_events_must_be_shared": True,
            "no_supported_relationship_is_allowed": True,
            "requires_evidence_review": False,
        },
    }


def build_relationship_repair_schema(
    context: dict[str, Any],
) -> dict[str, Any]:
    subject_id = context["repair_contract"]["subject_character_id"]
    partner_ids = [
        item["character_id"]
        for item in context.get("partner_candidates", [])
        if isinstance(item, dict) and isinstance(item.get("character_id"), str)
    ]
    event_ids = sorted(
        {
            event_id
            for item in context.get("partner_candidates", [])
            if isinstance(item, dict)
            for event_id in item.get("shared_event_ids", []) or []
            if isinstance(event_id, str)
        }
    )
    event_id_schema: dict[str, Any] = {
        "type": "string",
        "enum": event_ids or [""],
    }
    return {
        "type": "object",
        "properties": {
            "schema_version": {"type": "string", "const": "1.0"},
            "subject_character_id": {
                "type": "string",
                "const": subject_id,
            },
            "decision": {
                "type": "string",
                "enum": [
                    "relationship_found",
                    "no_supported_relationship",
                ],
            },
            "partner_character_id": {
                "type": "string",
                "enum": ["", *partner_ids],
            },
            "initial_state": {"type": "string"},
            "state_changes": {
                "type": "array",
                "maxItems": MAX_SHARED_EVENTS_PER_PARTNER,
                "items": {
                    "type": "object",
                    "properties": {
                        "event_id": event_id_schema,
                        "state": {"type": "string", "minLength": 1},
                        "reason": {"type": "string", "minLength": 1},
                    },
                    "required": ["event_id", "state", "reason"],
                    "additionalProperties": False,
                },
            },
        },
        "required": [
            "schema_version",
            "subject_character_id",
            "decision",
            "partner_character_id",
            "initial_state",
            "state_changes",
        ],
        "additionalProperties": False,
    }


def validate_relationship_repair_response(
    response: dict[str, Any],
    *,
    context: dict[str, Any],
    schema: dict[str, Any],
) -> list[str]:
    errors = validate_schema(response, schema)
    if errors:
        return errors
    decision = response.get("decision")
    partner_id = response.get("partner_character_id")
    state_changes = response.get("state_changes") or []
    if decision == "no_supported_relationship":
        if partner_id != "":
            errors.append(
                "no_supported_relationship requires empty partner_character_id"
            )
        if response.get("initial_state") != "":
            errors.append(
                "no_supported_relationship requires empty initial_state"
            )
        if state_changes:
            errors.append(
                "no_supported_relationship requires empty state_changes"
            )
        return errors
    if not partner_id:
        errors.append("relationship_found requires partner_character_id")
        return errors
    if not str(response.get("initial_state") or "").strip():
        errors.append("relationship_found requires non-empty initial_state")
    if not state_changes:
        errors.append("relationship_found requires at least one state_change")
        return errors
    candidate = next(
        (
            item
            for item in context.get("partner_candidates", [])
            if item.get("character_id") == partner_id
        ),
        None,
    )
    if not isinstance(candidate, dict):
        errors.append(f"partner is not allowed by repair context: {partner_id}")
        return errors
    shared_ids = set(candidate.get("shared_event_ids", []) or [])
    invalid_events = sorted(
        {
            item.get("event_id")
            for item in state_changes
            if isinstance(item, dict)
            and item.get("event_id") not in shared_ids
        }
    )
    if invalid_events:
        errors.append(
            "relationship repair references non-shared Event IDs: "
            + ", ".join(str(item) for item in invalid_events)
        )
    return errors


def _relationship_id(
    registry: dict[str, Any],
    *,
    subject_id: str,
    partner_id: str,
    state_changes: list[dict[str, Any]],
) -> str:
    value = {
        "character_ids": sorted((subject_id, partner_id)),
        "event_ids": sorted(
            item["event_id"]
            for item in state_changes
            if isinstance(item, dict) and isinstance(item.get("event_id"), str)
        ),
    }
    candidate = stable_id("rel-repair", value)
    existing = {
        item.get("id")
        for item in registry.get("relationships", []) or []
        if isinstance(item, dict)
    }
    if candidate not in existing:
        return candidate
    return stable_id(
        "rel-repair",
        {**value, "existing_relationship_ids": sorted(str(item) for item in existing)},
    )


def _context_has_strong_evidence(context: dict[str, Any]) -> bool:
    return any(
        bool(item.get("strong_evidence"))
        for item in context.get("partner_candidates", []) or []
        if isinstance(item, dict)
    )


def _review_context(
    context: dict[str, Any],
    *,
    reason: str,
    previous_response: dict[str, Any],
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    reviewed = copy.deepcopy(context)
    reviewed["repair_contract"]["requires_evidence_review"] = True
    reviewed["repair_contract"]["review_reason"] = reason
    reviewed["repair_contract"]["previous_response_sha256"] = json_sha256(
        previous_response
    )
    reviewed["repair_contract"]["previous_validation_errors"] = list(
        validation_errors or []
    )
    return reviewed


def repair_series_registry_relationships(
    registry: dict[str, Any],
    *,
    event_index: list[dict[str, Any]],
    resolver: RepairResolver,
    max_repair_jobs: int = MAX_REPAIR_JOBS,
) -> SeriesRegistryRelationshipRepairResult:
    raw = copy.deepcopy(registry)
    effective = copy.deepcopy(registry)
    repairs: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    attempted_without_support: set[str] = set()

    for repair_index in range(1, max_repair_jobs + 1):
        uncovered, other_errors = _relationship_findings(effective)
        if other_errors:
            return SeriesRegistryRelationshipRepairResult(
                raw,
                effective,
                tuple(repairs),
                tuple(decisions),
                tuple(other_errors),
            )
        remaining = [
            item for item in uncovered if item not in attempted_without_support
        ]
        if not remaining:
            break
        contexts = [
            build_relationship_repair_context(
                effective,
                event_index=event_index,
                subject_character_id=subject_id,
            )
            for subject_id in remaining
        ]
        contexts.sort(
            key=lambda item: (
                -sum(
                    int(candidate.get("shared_event_count", 0))
                    for candidate in item.get("partner_candidates", [])
                ),
                item["repair_contract"]["subject_character_id"],
            )
        )
        context = contexts[0]
        subject_id = context["repair_contract"]["subject_character_id"]
        if not context.get("partner_candidates"):
            decisions.append(
                {
                    "job_id": None,
                    "subject_character_id": subject_id,
                    "decision": "no_shared_event_partner_candidates",
                }
            )
            attempted_without_support.add(subject_id)
            continue
        schema = build_relationship_repair_schema(context)
        job_id = f"registry-relationship-{subject_id}-{repair_index:02d}"
        try:
            response = resolver(job_id, context, schema)
        except Exception as exc:
            decisions.append(
                {
                    "job_id": job_id,
                    "subject_character_id": subject_id,
                    "decision": "repair_job_failed",
                    "error": str(exc),
                }
            )
            attempted_without_support.add(subject_id)
            continue
        response_errors = validate_relationship_repair_response(
            response,
            context=context,
            schema=schema,
        )
        reviewed = False
        review_reason: str | None = None
        if response_errors:
            decisions.append(
                {
                    "job_id": job_id,
                    "subject_character_id": subject_id,
                    "decision": "invalid_repair_response",
                    "errors": response_errors,
                    "response_sha256": json_sha256(response),
                }
            )
            review_reason = "invalid_repair_response"
        elif (
            response.get("decision") == "no_supported_relationship"
            and _context_has_strong_evidence(context)
        ):
            decisions.append(
                {
                    "job_id": job_id,
                    "subject_character_id": subject_id,
                    "decision": "no_supported_relationship_pending_review",
                    "response_sha256": json_sha256(response),
                }
            )
            review_reason = "strong_evidence_requires_review"

        if review_reason is not None:
            review_job_id = f"{job_id}-review"
            reviewed_context = _review_context(
                context,
                reason=review_reason,
                previous_response=response,
                validation_errors=response_errors,
            )
            try:
                response = resolver(review_job_id, reviewed_context, schema)
            except Exception as exc:
                decisions.append(
                    {
                        "job_id": review_job_id,
                        "subject_character_id": subject_id,
                        "decision": "repair_review_failed",
                        "review_status": "failed",
                        "error": str(exc),
                    }
                )
                attempted_without_support.add(subject_id)
                continue
            response_errors = validate_relationship_repair_response(
                response,
                context=reviewed_context,
                schema=schema,
            )
            if response_errors:
                decisions.append(
                    {
                        "job_id": review_job_id,
                        "subject_character_id": subject_id,
                        "decision": "invalid_repair_review_response",
                        "review_status": "failed",
                        "errors": response_errors,
                        "response_sha256": json_sha256(response),
                    }
                )
                attempted_without_support.add(subject_id)
                continue
            context = reviewed_context
            job_id = review_job_id
            reviewed = True

        decision = str(response["decision"])
        if decision == "no_supported_relationship":
            decisions.append(
                {
                    "job_id": job_id,
                    "subject_character_id": subject_id,
                    "decision": decision,
                    "evidence_reviewed": reviewed,
                    "review_status": (
                        "completed"
                        if reviewed
                        else "not_required_weak_evidence"
                    ),
                    "response_sha256": json_sha256(response),
                }
            )
            attempted_without_support.add(subject_id)
            continue
        partner_id = str(response["partner_character_id"])
        state_changes = copy.deepcopy(response["state_changes"])
        relationship_id = _relationship_id(
            effective,
            subject_id=subject_id,
            partner_id=partner_id,
            state_changes=state_changes,
        )
        relationship = {
            "id": relationship_id,
            "character_ids": sorted((subject_id, partner_id)),
            "initial_state": response["initial_state"],
            "state_changes": state_changes,
        }
        effective.setdefault("relationships", []).append(relationship)
        repair = {
            "action": "add_model_evidenced_relationship",
            "job_id": job_id,
            "relationship_id": relationship_id,
            "subject_character_id": subject_id,
            "partner_character_id": partner_id,
            "evidence_event_ids": [
                item["event_id"] for item in state_changes
            ],
            "response_sha256": json_sha256(response),
        }
        repairs.append(repair)
        decisions.append({**repair, "decision": decision})

    remaining_result = validate_series_registry_contract(
        effective,
        event_index=event_index,
    )
    errors = tuple(remaining_result.errors)
    if not errors and not repairs:
        errors = ("relationship repair made no evidence-backed changes",)
    return SeriesRegistryRelationshipRepairResult(
        raw,
        effective,
        tuple(repairs),
        tuple(decisions),
        errors,
    )