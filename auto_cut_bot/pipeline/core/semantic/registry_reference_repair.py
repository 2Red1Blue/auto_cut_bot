#!/usr/bin/env python3
"""Deletion-only repair for dangling optional Series Registry references."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from autocut_core.io import json_sha256
from .registry_contract import validate_series_registry_contract


POLICY_VERSION = "series-registry-reference-deletion-repair-v1"


@dataclass(frozen=True)
class SeriesRegistryReferenceRepairResult:
    raw_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    repairs: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]

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
            "status": "repaired" if not self.errors else "partially_repaired",
            "raw_registry_sha256": self.raw_sha256,
            "effective_registry_sha256": self.effective_sha256,
            "repair_count": len(self.repairs),
            "repairs": list(self.repairs),
            "blocking_errors": list(self.errors),
        }


def canonicalize_series_registry_references(
    registry: dict[str, Any],
    *,
    known_event_ids: set[str] | None = None,
) -> SeriesRegistryReferenceRepairResult:
    """Remove only dangling ``story_threads.open_question_ids`` entries.

    The missing object carries no recoverable semantic payload, so deleting
    the optional reference is deterministic.  Unknown character and Event
    references remain blocking findings and are never guessed or deleted.
    """

    raw = copy.deepcopy(registry)
    effective = copy.deepcopy(registry)
    known_question_ids = {
        item["id"]
        for item in effective.get("open_questions", []) or []
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    repairs: list[dict[str, Any]] = []
    for thread in effective.get("story_threads", []) or []:
        if not isinstance(thread, dict):
            continue
        question_ids = thread.get("open_question_ids")
        if not isinstance(question_ids, list):
            continue
        kept = [
            item
            for item in question_ids
            if not isinstance(item, str) or item in known_question_ids
        ]
        removed = sorted(
            {
                item
                for item in question_ids
                if isinstance(item, str) and item not in known_question_ids
            }
        )
        if not removed:
            continue
        thread["open_question_ids"] = kept
        repairs.append(
            {
                "action": "remove_dangling_open_question_references",
                "thread_id": thread.get("id"),
                "removed_open_question_ids": removed,
            }
        )

    remaining = validate_series_registry_contract(
        effective,
        known_event_ids=known_event_ids,
    )
    return SeriesRegistryReferenceRepairResult(
        raw_registry=raw,
        effective_registry=effective,
        repairs=tuple(repairs),
        errors=tuple(remaining.errors),
    )