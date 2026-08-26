"""Closed Stage 3 decision content; construction is never an authority grant.

Only the Command can supply the three committed-input/request-audit checks.
The registered strategy makes no token, partition or physical-capacity claim.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

from ..contracts.compiler.canonical import canonical_json_hash
from .editorial_feasibility import EditorialFeasibilityResult
from .editorial_members import EDITORIAL_STORY_MEMBER_TYPES
from .editorial_models import editorial_array, editorial_hash, editorial_mapping, editorial_tuple
from .member_refs import SemanticMemberIdentity

SS_EVALUATION_STRATEGY = "stage3-ss-unpartitioned-v1"
SS_BATCH_RULE_IDS = (
    "SS-IN-001", "SS-IN-002", "SS-BATCH-001", "SS-CTX-BYTES-001", "SS-REUSE-001", "SS-SEARCH-001",
)
SS_COMMAND_RULE_IDS = ("SS-IN-001", "SS-IN-002", "SS-CTX-BYTES-001")
SS_BUSINESS_BATCH_RULE_IDS = ("SS-BATCH-001", "SS-REUSE-001", "SS-SEARCH-001")
SS_STORY_RULE_IDS = (
    "SS-REF-001", "SS-ENUM-001", "SS-OBL-001", "SS-EV-001", "SS-EV-002", "SS-CAND-CAP-001",
    "SS-PHYS-DEFER-001", "SS-PREF-001", "SS-SPAN-001", "SS-CTX-001", "SS-CTX-002",
    "SS-HASH-001", "SS-DUR-002", "SS-TAINT-001",
)
SS_RULE_IDS = SS_BATCH_RULE_IDS + SS_STORY_RULE_IDS
_STOP_ON_FAIL = frozenset((*SS_COMMAND_RULE_IDS, "SS-BATCH-001", "SS-REF-001", "SS-ENUM-001",
                           "SS-HASH-001", "SS-PHYS-DEFER-001"))
_HASH_FIELDS = ("input_binding_sha256", "raw_draft_sha256", "canonical_draft_sha256",
                "command_policy_sha256", "stage2_policy_sha256")


@dataclass(frozen=True, slots=True)
class EditorialCheck:
    rule_id: str
    status: str
    violation_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.rule_id) is not str or self.rule_id not in SS_RULE_IDS:  # noqa: E721
            raise ValueError("unregistered Stage 3 rule")
        if type(self.status) is not str or self.status not in ("pass", "fail", "indeterminate"):  # noqa: E721
            raise ValueError("invalid Stage 3 check status")
        codes = editorial_tuple(self.violation_codes, str)
        if any(re.fullmatch(r"[a-z][a-z0-9_]*", code) is None for code in codes):
            raise ValueError("Stage 3 violations must be stable codes")
        if tuple(sorted(set(codes))) != codes or (self.status == "pass") != (not codes):
            raise ValueError("Stage 3 violations must be unique canonical reasons for non-pass")

    def to_mapping(self) -> dict[str, object]:
        return {"rule_id": self.rule_id, "status": self.status, "violation_codes": list(self.violation_codes)}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialCheck:
        item = editorial_mapping(value, ("rule_id", "status", "violation_codes"))
        return cls(cast(str, item["rule_id"]), cast(str, item["status"]),
                   editorial_array(item["violation_codes"], lambda code: cast(str, code)))


def _status(checks: tuple[EditorialCheck, ...]) -> str:
    statuses = {check.status for check in checks}
    return "invalid" if "fail" in statuses else "indeterminate" if "indeterminate" in statuses else "valid"


def _action(checks: tuple[EditorialCheck, ...]) -> str:
    actions = {
        ("stop" if check.rule_id in _STOP_ON_FAIL else "repair") if check.status == "fail"
        else ("stop" if check.rule_id in SS_COMMAND_RULE_IDS else "quarantine")
        for check in checks if check.status != "pass"
    }
    return next((action for action in ("stop", "repair", "quarantine") if action in actions), "continue")


@dataclass(frozen=True, slots=True)
class EditorialStoryDecision:
    story_id: str
    checks: tuple[EditorialCheck, ...]

    def __post_init__(self) -> None:
        editorial_hash(self.story_id)
        editorial_tuple(self.checks, EditorialCheck)
        if tuple(check.rule_id for check in self.checks) != SS_STORY_RULE_IDS:
            raise ValueError("Stage 3 Story requires the exact ordered fourteen checks")

    @property
    def validation_status(self) -> str:
        return _status(self.checks)

    @property
    def next_action(self) -> str:
        return _action(self.checks)

    def to_mapping(self) -> dict[str, object]:
        return {"story_id": self.story_id, "checks": [check.to_mapping() for check in self.checks],
                "validation_status": self.validation_status, "next_action": self.next_action}

    @classmethod
    def from_mapping(cls, value: object) -> EditorialStoryDecision:
        item = editorial_mapping(value, ("story_id", "checks", "validation_status", "next_action"))
        result = cls(editorial_hash(item["story_id"]), editorial_array(item["checks"], EditorialCheck.from_mapping))
        if item["validation_status"] != result.validation_status or item["next_action"] != result.next_action:
            raise ValueError("Stage 3 Story derived decision differs")
        return result


@dataclass(frozen=True, slots=True)
class SemanticFeasibilityAdmission:
    input_binding_sha256: str
    raw_draft_sha256: str
    canonical_draft_sha256: str
    command_policy_sha256: str
    stage2_policy_sha256: str
    feasibility: EditorialFeasibilityResult
    business_members: tuple[SemanticMemberIdentity, ...]
    stories: tuple[EditorialStoryDecision, ...]
    checks: tuple[EditorialCheck, ...]
    evaluation_strategy_version: str

    def __post_init__(self) -> None:
        for name in _HASH_FIELDS:
            editorial_hash(getattr(self, name))
        if type(self.evaluation_strategy_version) is not str or self.evaluation_strategy_version != SS_EVALUATION_STRATEGY:  # noqa: E721
            raise ValueError("unsupported Stage 3 evaluation strategy")
        if type(self.feasibility) is not EditorialFeasibilityResult:  # noqa: E721
            raise ValueError("Stage 3 requires the full typed feasibility result")
        editorial_tuple(self.stories, EditorialStoryDecision, nonempty=True)
        if len(set(self.target_story_ids)) != len(self.stories):
            raise ValueError("Stage 3 frozen targets must be unique")
        if tuple(row.story_id for row in self.feasibility.timing_witnesses) != self.target_story_ids:
            raise ValueError("Stage 3 timing targets differ from frozen order")
        editorial_tuple(self.business_members, SemanticMemberIdentity)
        if len(self.business_members) != 3 * len(self.stories):
            raise ValueError("Stage 3 Admission requires exactly 3N business subjects")
        for index, story in enumerate(self.stories):
            for offset, kind in enumerate(EDITORIAL_STORY_MEMBER_TYPES):
                member = self.business_members[index * 3 + offset]
                if member.artifact_type != kind or member.logical_id != f"{kind}@{story.story_id}":
                    raise ValueError("Stage 3 business subject type/order/Story differs")
        if len({(member.scope, member.revision) for member in self.business_members}) != 1:
            raise ValueError("Stage 3 business subjects mix scope/revision")
        editorial_tuple(self.checks, EditorialCheck)
        if tuple(check.rule_id for check in self.checks) != SS_BATCH_RULE_IDS:
            raise ValueError("Stage 3 batch requires the exact ordered six checks")
        search = self.feasibility.material_search
        if search.status != "feasible" and next(check for check in self.checks if check.rule_id == "SS-SEARCH-001").status == "pass":
            raise ValueError("non-feasible material search cannot carry a passing search check")
        if search.status == "feasible" and {choice.story_id for choice in search.choices} != set(self.target_story_ids):
            raise ValueError("feasible material choices must cover exactly the frozen Story targets")
        for story, timing in zip(self.stories, self.feasibility.timing_witnesses, strict=True):
            if timing.durations is None and next(check for check in story.checks if check.rule_id == "SS-DUR-002").status == "pass":
                raise ValueError("absent timing witness cannot carry a passing duration check")

    @property
    def target_story_ids(self) -> tuple[str, ...]:
        return tuple(story.story_id for story in self.stories)

    @property
    def subject_hash(self) -> str:
        return canonical_json_hash([member.to_mapping() for member in self.business_members])

    @property
    def feasibility_sha256(self) -> str:
        return self.feasibility.canonical_hash

    @property
    def validation_status(self) -> str:
        return _status(self.checks + tuple(check for story in self.stories for check in story.checks))

    @property
    def next_action(self) -> str:
        return _action(self.checks + tuple(check for story in self.stories for check in story.checks))

    def to_mapping(self) -> dict[str, object]:
        return {
            "schema_version": "stage3-semantic-feasibility-admission-v1",
            **{name: getattr(self, name) for name in _HASH_FIELDS},
            "evaluation_strategy_version": self.evaluation_strategy_version,
            "feasibility": self.feasibility.to_mapping(), "feasibility_sha256": self.feasibility_sha256,
            "business_members": [member.to_mapping() for member in self.business_members],
            "subject_hash": self.subject_hash, "target_story_ids": list(self.target_story_ids),
            "stories": [story.to_mapping() for story in self.stories],
            "checks": [check.to_mapping() for check in self.checks],
            "validation_status": self.validation_status, "next_action": self.next_action,
        }

    @classmethod
    def from_mapping(cls, value: object) -> SemanticFeasibilityAdmission:
        item = editorial_mapping(value, ("schema_version", *_HASH_FIELDS, "evaluation_strategy_version",
                                         "feasibility", "feasibility_sha256", "business_members", "subject_hash",
                                         "target_story_ids", "stories", "checks", "validation_status", "next_action"))
        if item["schema_version"] != "stage3-semantic-feasibility-admission-v1":
            raise ValueError("unsupported Stage 3 Admission schema")
        result = cls(
            editorial_hash(item["input_binding_sha256"]), editorial_hash(item["raw_draft_sha256"]),
            editorial_hash(item["canonical_draft_sha256"]), editorial_hash(item["command_policy_sha256"]),
            editorial_hash(item["stage2_policy_sha256"]),
            EditorialFeasibilityResult.from_mapping(item["feasibility"]),
            editorial_array(item["business_members"], SemanticMemberIdentity.from_mapping),
            editorial_array(item["stories"], EditorialStoryDecision.from_mapping),
            editorial_array(item["checks"], EditorialCheck.from_mapping), cast(str, item["evaluation_strategy_version"]),
        )
        if (item["subject_hash"] != result.subject_hash or item["feasibility_sha256"] != result.feasibility_sha256
                or editorial_array(item["target_story_ids"], editorial_hash) != result.target_story_ids
                or item["validation_status"] != result.validation_status or item["next_action"] != result.next_action):
            raise ValueError("Stage 3 Admission derived identity/decision differs")
        return result

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())
