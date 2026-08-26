"""Closed final Stage 1 Admission content; not a caller authorization token.

Only the Command may commit this value after independently evaluating all rules
against exact Store inputs and its durable raw response. Public construction or
successful decoding proves structure, never the truth of caller-supplied checks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from .member_refs import SemanticMemberIdentity
from .stage1_checks import KC_RULE_IDS, Stage1Check
from .stage1_members import COVERAGE_MEMBER_TYPES

BUSINESS_MEMBER_TYPES = (*COVERAGE_MEMBER_TYPES, "dependency_closure_proof")
_HASH_FIELDS = (
    "input_binding_sha256", "raw_draft_sha256", "canonical_draft_sha256",
    "draft_policy_sha256", "coverage_policy_sha256", "dependency_policy_sha256",
)
_REPAIR_ON_FAIL = frozenset({
    "KC-GRAPH-001", "KC-GRAPH-002", "KC-COV-001", "KC-COV-002", "KC-COV-003",
    "KC-COV-004", "KC-COV-005", "KC-EXCLUDE-001",
})
_QUARANTINE_ON_FAIL = frozenset({"KC-DEP-003", "KC-GATE-001"})
_QUARANTINE_ON_UNKNOWN = frozenset({
    "KC-GRAPH-002", "KC-EXCLUDE-001", "KC-AUTH-001", "KC-AUTH-002",
    "KC-DEP-003", "KC-GATE-001",
})


def _hash(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:  # noqa: E721
        raise ValueError("Admission hash must be lowercase sha256")
    return value


@dataclass(frozen=True, slots=True)
class CoverageAdmission:
    coverage_admission_id: str
    input_binding_sha256: str
    raw_draft_sha256: str
    canonical_draft_sha256: str
    draft_policy_sha256: str
    coverage_policy_sha256: str
    dependency_policy_sha256: str
    coverage_mode: str
    evaluation_strategy_version: str
    business_members: tuple[SemanticMemberIdentity, ...]
    rule_results: tuple[Stage1Check, ...]

    def __post_init__(self) -> None:
        if type(self.coverage_admission_id) is not str or not self.coverage_admission_id.strip():  # noqa: E721
            raise ValueError("Admission ID must be nonempty UTF-8")
        self.coverage_admission_id.encode("utf-8")
        for field in _HASH_FIELDS:
            _hash(getattr(self, field))
        if type(self.coverage_mode) is not str or self.coverage_mode != "strict_global":  # noqa: E721
            raise ValueError("only the implemented strict_global Admission is allowed")
        if type(self.evaluation_strategy_version) is not str or self.evaluation_strategy_version != "stage1-kc-v1":  # noqa: E721
            raise ValueError("Admission requires the explicit implemented KC strategy")
        if type(self.business_members) is not tuple or any(  # noqa: E721
            type(item) is not SemanticMemberIdentity for item in self.business_members
        ):
            raise ValueError("business subject requires exact member identities")
        if len(self.business_members) != 7 or {item.artifact_type for item in self.business_members} != set(BUSINESS_MEMBER_TYPES):
            raise ValueError("Admission subject must contain exactly seven business members")
        if len({(item.scope, item.revision) for item in self.business_members}) != 1 or any(
            item.logical_id != item.artifact_type for item in self.business_members
        ):
            raise ValueError("Admission business member identity mismatch")
        if type(self.rule_results) is not tuple or any(type(item) is not Stage1Check for item in self.rule_results):  # noqa: E721
            raise ValueError("Admission requires exact Stage1Check values")
        if len(self.rule_results) != len(KC_RULE_IDS) or {item.rule_id for item in self.rule_results} != set(KC_RULE_IDS):
            raise ValueError("Admission must retain all seventeen distinct KC checks")
        object.__setattr__(self, "business_members", tuple(sorted(
            self.business_members, key=lambda item: canonical_json_bytes(item.to_mapping()),
        )))
        object.__setattr__(self, "rule_results", tuple(sorted(self.rule_results, key=lambda item: item.rule_id)))

    @property
    def subject_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.business_members])

    @property
    def validation_status(self) -> str:
        statuses = {item.status for item in self.rule_results if item.rule_id != "KC-GATE-001"}
        if "fail" in statuses:
            return "invalid"
        return "indeterminate" if "indeterminate" in statuses else "valid"

    @property
    def next_action(self) -> str:
        actions: set[str] = set()
        for result in self.rule_results:
            if result.status == "pass":
                continue
            if result.status == "indeterminate":
                actions.add("quarantine" if result.rule_id in _QUARANTINE_ON_UNKNOWN else "stop")
            elif result.rule_id in _REPAIR_ON_FAIL:
                actions.add("repair")
            elif result.rule_id in _QUARANTINE_ON_FAIL:
                actions.add("quarantine")
            else:
                actions.add("stop")
        # Stop is terminal; an explicitly repairable failure may be recovered
        # before a strict-global taint quarantine. Recovery budgets belong to
        # the executor; this value alone does not schedule or authorize retries.
        for action in ("stop", "repair", "quarantine"):
            if action in actions:
                return action
        return "continue"

    def to_mapping(self) -> dict[str, object]:
        return {
            "coverage_admission_id": self.coverage_admission_id,
            **{field: getattr(self, field) for field in _HASH_FIELDS},
            "coverage_mode": self.coverage_mode,
            "evaluation_strategy_version": self.evaluation_strategy_version,
            "business_members": [item.to_mapping() for item in self.business_members],
            "subject_hash": self.subject_hash,
            "rule_results": [{**item.to_mapping(), "subject_hash": self.subject_hash} for item in self.rule_results],
            "validation_status": self.validation_status, "next_action": self.next_action,
        }

    @classmethod
    def from_mapping(cls, value: object) -> CoverageAdmission:
        if type(value) is not dict:  # noqa: E721
            raise ValueError("Admission must be a closed JSON object")
        item = cast(dict[str, object], value)
        expected = {
            "coverage_admission_id", *_HASH_FIELDS, "coverage_mode", "evaluation_strategy_version", "business_members",
            "subject_hash", "rule_results", "validation_status", "next_action",
        }
        if set(item) != expected or type(item["business_members"]) is not list or type(item["rule_results"]) is not list:
            raise ValueError("Admission has missing or unknown fields")
        subject = _hash(item["subject_hash"])
        rules: list[Stage1Check] = []
        for raw in cast(list[object], item["rule_results"]):
            if type(raw) is not dict:  # noqa: E721
                raise ValueError("Admission rule must be a closed object")
            rule = cast(dict[str, object], raw)
            if set(rule) != {"rule_id", "status", "violation_codes", "subject_hash"} or rule["subject_hash"] != subject:
                raise ValueError("Admission rule does not bind the exact business subject")
            rules.append(Stage1Check.from_mapping({key: field for key, field in rule.items() if key != "subject_hash"}))
        result = cls(
            cast(str, item["coverage_admission_id"]),
            _hash(item["input_binding_sha256"]), _hash(item["raw_draft_sha256"]),
            _hash(item["canonical_draft_sha256"]), _hash(item["draft_policy_sha256"]),
            _hash(item["coverage_policy_sha256"]), _hash(item["dependency_policy_sha256"]),
            cast(str, item["coverage_mode"]),
            cast(str, item["evaluation_strategy_version"]),
            tuple(SemanticMemberIdentity.from_mapping(member) for member in cast(list[object], item["business_members"])),
            tuple(rules),
        )
        if (subject != result.subject_hash or item["validation_status"] != result.validation_status
                or item["next_action"] != result.next_action):
            raise ValueError("Admission derived subject/status/action differs from actual content")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
