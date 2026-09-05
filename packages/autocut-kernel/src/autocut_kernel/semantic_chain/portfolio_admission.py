"""Closed Stage 2 decision content, never caller-supplied authority.

All nineteen checks must be independently performed before Command commit.
Constructing or decoding this value proves only structural consistency.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from .member_refs import SemanticMemberIdentity
from .story_design_compiler import STAGE2_BUSINESS_MEMBER_TYPES

SD_RULE_IDS = (
    "SD-IN-001", "SD-IN-002", "SD-PROP-001", "SD-REF-001", "SD-ENUM-001",
    "SD-DUR-001", "SD-MAT-001", "SD-MAT-002", "SD-PHYS-DEFER-001",
    "SD-CAND-SEM-001", "SD-CAND-CAP-001", "SD-TAINT-001", "SD-TAINT-002",
    "SD-PORT-001", "SD-PORT-002", "SD-PORT-003", "SD-OBJ-001", "SD-FREEZE-001",
    "SD-USAGE-001",
)
_HASH_FIELDS = (
    "input_binding_sha256", "raw_draft_sha256", "canonical_draft_sha256",
    "draft_policy_sha256", "candidate_policy_sha256", "story_policy_sha256", "job_policy_sha256",
)
_REPAIR_ON_FAIL = frozenset({
    "SD-PROP-001", "SD-REF-001", "SD-ENUM-001", "SD-DUR-001", "SD-MAT-002",
    "SD-TAINT-001", "SD-PORT-001", "SD-PORT-002", "SD-PORT-003",
})
_QUARANTINE_ON_UNKNOWN = frozenset({
    "SD-MAT-002", "SD-CAND-SEM-001", "SD-CAND-CAP-001", "SD-TAINT-001",
    "SD-TAINT-002", "SD-PORT-002", "SD-PORT-003",
})


def _hash(value: object) -> str:
    if type(value) is not str or re.fullmatch(r"sha256:[0-9a-f]{64}", value) is None:  # noqa: E721
        raise ValueError("Stage 2 Admission hash must be lowercase sha256")
    return value


def _closed(value: object, fields: set[str]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise ValueError("Stage 2 decision value must be a closed object")
    item = cast(dict[str, object], value)
    if set(item) != fields or any(type(key) is not str for key in item):  # noqa: E721
        raise ValueError("Stage 2 decision value has missing or unknown fields")
    return item


def _array(value: object) -> list[object]:
    if type(value) is not list:  # noqa: E721
        raise ValueError("Stage 2 decision collection must be an array")
    return cast(list[object], value)


@dataclass(frozen=True, slots=True)
class Stage2Check:
    rule_id: str
    status: str
    violation_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.rule_id) is not str or self.rule_id not in SD_RULE_IDS:  # noqa: E721
            raise ValueError("unknown Stage 2 rule")
        if type(self.status) is not str or self.status not in ("pass", "fail", "indeterminate"):  # noqa: E721
            raise ValueError("unknown Stage 2 rule status")
        if type(self.violation_codes) is not tuple or any(  # noqa: E721
            type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]*", code) is None
            for code in self.violation_codes
        ):
            raise ValueError("Stage 2 violation codes must be exact stable text")
        if tuple(sorted(set(self.violation_codes))) != self.violation_codes:
            raise ValueError("Stage 2 violation codes must be canonical and unique")
        if (self.status == "pass") != (not self.violation_codes):
            raise ValueError("non-pass requires a reason; pass cannot have violations")

    def to_mapping(self) -> dict[str, object]:
        return {"rule_id": self.rule_id, "status": self.status, "violation_codes": list(self.violation_codes)}

    @classmethod
    def from_mapping(cls, value: object) -> Stage2Check:
        item = _closed(value, {"rule_id", "status", "violation_codes"})
        # Exact primitive checks are performed by the constructor, not the cast.
        return cls(cast(str, item["rule_id"]), cast(str, item["status"]),
                   tuple(cast(list[str], _array(item["violation_codes"]))))


@dataclass(frozen=True, slots=True)
class PortfolioAdmission:
    input_binding_sha256: str
    raw_draft_sha256: str
    canonical_draft_sha256: str
    draft_policy_sha256: str
    candidate_policy_sha256: str
    story_policy_sha256: str
    job_policy_sha256: str
    evaluation_strategy_version: str
    business_members: tuple[SemanticMemberIdentity, ...]
    target_story_ids: tuple[str, ...]
    rule_results: tuple[Stage2Check, ...]

    def __post_init__(self) -> None:
        for field in _HASH_FIELDS:
            _hash(getattr(self, field))
        if type(self.evaluation_strategy_version) is not str or self.evaluation_strategy_version not in ("stage2-sd-v1", "stage2-sd-compact-v2"):  # noqa: E721
            raise ValueError("unsupported Stage 2 evaluation strategy")
        if type(self.business_members) is not tuple or any(type(item) is not SemanticMemberIdentity for item in self.business_members):  # noqa: E721
            raise ValueError("Stage 2 Admission requires exact member identities")
        if len(self.business_members) != 4 or {item.artifact_type for item in self.business_members} != set(STAGE2_BUSINESS_MEMBER_TYPES):
            raise ValueError("Stage 2 Admission must bind exactly four business subjects")
        if len({(item.scope, item.revision) for item in self.business_members}) != 1 or any(
            item.logical_id != item.artifact_type for item in self.business_members
        ):
            raise ValueError("Stage 2 Admission subject identity mismatch")
        if type(self.target_story_ids) is not tuple or not self.target_story_ids:  # noqa: E721
            raise ValueError("Stage 2 Admission requires a nonempty frozen target order")
        for story in self.target_story_ids:
            _hash(story)
        if len(set(self.target_story_ids)) != len(self.target_story_ids):
            raise ValueError("Stage 2 Admission targets must be unique")
        if type(self.rule_results) is not tuple or any(type(item) is not Stage2Check for item in self.rule_results):  # noqa: E721
            raise ValueError("Stage 2 Admission requires exact Stage2Check values")
        if len(self.rule_results) != len(SD_RULE_IDS) or {item.rule_id for item in self.rule_results} != set(SD_RULE_IDS):
            raise ValueError("Stage 2 Admission requires all nineteen distinct checks")
        object.__setattr__(self, "business_members", tuple(sorted(self.business_members,
                           key=lambda item: canonical_json_bytes(item.to_mapping()))))
        object.__setattr__(self, "rule_results", tuple(sorted(self.rule_results, key=lambda item: item.rule_id)))

    @property
    def subject_hash(self) -> str:
        return canonical_json_hash([item.to_mapping() for item in self.business_members])

    @property
    def target_story_ids_hash(self) -> str:
        return canonical_json_hash(list(self.target_story_ids))

    @property
    def validation_status(self) -> str:
        statuses = {item.status for item in self.rule_results}
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
            else:
                actions.add("repair" if result.rule_id in _REPAIR_ON_FAIL else "stop")
        # Retry limits/exhaustion are executor policy, never authorized here.
        return next((action for action in ("stop", "repair", "quarantine") if action in actions), "continue")

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "stage2-portfolio-admission-v1",
            **{field: getattr(self, field) for field in _HASH_FIELDS},
            "evaluation_strategy_version": self.evaluation_strategy_version,
            "business_members": [item.to_mapping() for item in self.business_members],
            "subject_hash": self.subject_hash,
            "target_story_ids": list(self.target_story_ids), "target_story_ids_hash": self.target_story_ids_hash,
            "rule_results": [{**item.to_mapping(), "subject_hash": self.subject_hash} for item in self.rule_results],
            "validation_status": self.validation_status, "next_action": self.next_action,
        }

    @classmethod
    def from_mapping(cls, value: object) -> PortfolioAdmission:
        item = _closed(value, {"schema_version", *_HASH_FIELDS, "evaluation_strategy_version", "business_members",
                               "subject_hash", "target_story_ids", "target_story_ids_hash", "rule_results",
                               "validation_status", "next_action"})
        if type(item["schema_version"]) is not str or item["schema_version"] != "stage2-portfolio-admission-v1":  # noqa: E721
            raise ValueError("unsupported Stage 2 Admission schema")
        subject = _hash(item["subject_hash"])
        checks: list[Stage2Check] = []
        for raw in _array(item["rule_results"]):
            row = _closed(raw, {"rule_id", "status", "violation_codes", "subject_hash"})
            if _hash(row["subject_hash"]) != subject:
                raise ValueError("Stage 2 rule names a different subject")
            checks.append(Stage2Check.from_mapping({key: value for key, value in row.items() if key != "subject_hash"}))
        result = cls(
            _hash(item["input_binding_sha256"]), _hash(item["raw_draft_sha256"]), _hash(item["canonical_draft_sha256"]),
            _hash(item["draft_policy_sha256"]), _hash(item["candidate_policy_sha256"]), _hash(item["story_policy_sha256"]),
            _hash(item["job_policy_sha256"]), cast(str, item["evaluation_strategy_version"]),
            tuple(SemanticMemberIdentity.from_mapping(member) for member in _array(item["business_members"])),
            tuple(_hash(story) for story in _array(item["target_story_ids"])), tuple(checks),
        )
        if (result.subject_hash != subject or _hash(item["target_story_ids_hash"]) != result.target_story_ids_hash
                or item["validation_status"] != result.validation_status or item["next_action"] != result.next_action):
            raise ValueError("Stage 2 Admission derived identity/decision differs from content")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
