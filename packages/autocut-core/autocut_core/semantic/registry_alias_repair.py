#!/usr/bin/env python3
"""Deletion-only repair for ambiguous Series Registry aliases."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

from autocut_core.io import json_sha256
from .registry_contract import (
    ALIAS_CANONICAL_COLLISION,
    ALIAS_OWNER_COLLISION,
    normalize_character_name,
    validate_series_registry_contract,
)


POLICY_VERSION = "series-registry-alias-deletion-repair-v1"
REPAIRABLE_ALIAS_FINDINGS = frozenset(
    {
        ALIAS_CANONICAL_COLLISION,
        ALIAS_OWNER_COLLISION,
    }
)


@dataclass(frozen=True)
class SeriesRegistryAliasRepairResult:
    raw_registry: dict[str, Any]
    effective_registry: dict[str, Any]
    repairs: tuple[dict[str, Any], ...]
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
            "status": "repaired" if self.ok else "partially_repaired",
            "raw_registry_sha256": self.raw_sha256,
            "effective_registry_sha256": self.effective_sha256,
            "repair_count": len(self.repairs),
            "repairs": list(self.repairs),
            "blocking_errors": list(self.errors),
        }


def canonicalize_series_registry_aliases(
    registry: dict[str, Any],
) -> SeriesRegistryAliasRepairResult:
    """Remove only aliases identified by the cross-record alias contract."""

    raw = copy.deepcopy(registry)
    effective = copy.deepcopy(registry)
    initial = validate_series_registry_contract(raw)
    removal_targets: dict[str, dict[str, Any]] = {}
    for finding in initial.findings:
        if finding.code not in REPAIRABLE_ALIAS_FINDINGS:
            continue
        normalized = normalize_character_name(finding.value or "")
        if not normalized:
            continue
        target = removal_targets.setdefault(
            normalized,
            {
                "alias": finding.value,
                "finding_codes": set(),
                "finding_character_ids": set(),
            },
        )
        target["finding_codes"].add(finding.code)
        target["finding_character_ids"].update(finding.character_ids)

    removed_by_alias: dict[str, dict[str, Any]] = {}
    for character in effective.get("characters", []) or []:
        if not isinstance(character, dict):
            continue
        character_id = str(character.get("id") or "")
        aliases = character.get("aliases", []) or []
        kept_aliases: list[Any] = []
        for alias in aliases:
            normalized = normalize_character_name(alias)
            target = removal_targets.get(normalized)
            if (
                target is None
                or character_id not in target["finding_character_ids"]
            ):
                kept_aliases.append(alias)
                continue
            removed = removed_by_alias.setdefault(
                normalized,
                {
                    "action": "remove_ambiguous_alias",
                    "alias": str(alias),
                    "character_ids": [],
                    "finding_codes": sorted(target["finding_codes"]),
                },
            )
            removed["character_ids"].append(character_id)
        character["aliases"] = kept_aliases

    repairs = []
    for normalized in sorted(removed_by_alias):
        repair = removed_by_alias[normalized]
        repair["character_ids"] = sorted(set(repair["character_ids"]))
        repairs.append(repair)

    remaining = validate_series_registry_contract(effective)
    return SeriesRegistryAliasRepairResult(
        raw_registry=raw,
        effective_registry=effective,
        repairs=tuple(repairs),
        errors=tuple(remaining.errors),
    )