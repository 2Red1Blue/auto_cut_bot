"""Private, explicit rule evaluation for Stage 1 admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .authority import require_sha256

RuleStatus = Literal["indeterminate", "pass", "fail"]


@dataclass(frozen=True, slots=True)
class _RuleResult:
    """Evaluator-owned result; intentionally not exported as a public witness."""

    rule_id: str
    status: RuleStatus
    subject_sha256: str

    def __post_init__(self) -> None:
        if self.rule_id not in {
            "semantic_analysis_authorization",
            "semantic_input_resolution",
            "semantic_conflict_free",
            "coverage_universe_complete",
        }:
            raise ValueError("rule identifier is unsupported")
        if self.status not in {"indeterminate", "pass", "fail"}:
            raise ValueError("rule status is unsupported")
        require_sha256(self.subject_sha256, "rule subject")

    def to_mapping(self) -> dict[str, str]:
        return {
            "rule_id": self.rule_id,
            "status": self.status,
            "subject_sha256": self.subject_sha256,
        }


def indeterminate_rules(subject_sha256: str) -> tuple[_RuleResult, ...]:
    return tuple(
        _RuleResult(rule_id, "indeterminate", subject_sha256)
        for rule_id in (
            "semantic_analysis_authorization",
            "semantic_input_resolution",
            "semantic_conflict_free",
            "coverage_universe_complete",
        )
    )


def evaluate_rules(
    subject_sha256: str,
    *,
    authorized: bool,
    resolved: bool,
    conflict_free: bool,
    coverage_complete: bool,
) -> tuple[_RuleResult, ...]:
    """The only transition from indeterminate to pass/fail in this slice."""

    values = {
        "semantic_analysis_authorization": authorized,
        "semantic_input_resolution": resolved,
        "semantic_conflict_free": conflict_free,
        "coverage_universe_complete": coverage_complete,
    }
    return tuple(
        _RuleResult(rule_id, "pass" if value else "fail", subject_sha256)
        for rule_id, value in values.items()
    )
