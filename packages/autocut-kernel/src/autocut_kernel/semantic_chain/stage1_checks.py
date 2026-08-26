"""Stage 1 checked values. Constructing one is not a permission to commit."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import cast

KC_RULE_IDS = (
    "KC-IN-001", "KC-GRAPH-001", "KC-GRAPH-002", "KC-COV-001", "KC-COV-002",
    "KC-COV-003", "KC-COV-004", "KC-COV-005", "KC-EXCLUDE-001", "KC-AUTH-001",
    "KC-AUTH-002", "KC-EVENT-001", "KC-DEP-001", "KC-DEP-002", "KC-DEP-003",
    "KC-ISO-001", "KC-GATE-001",
)


@dataclass(frozen=True, slots=True)
class Stage1Check:
    rule_id: str
    status: str
    violation_codes: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.rule_id) is not str or self.rule_id not in KC_RULE_IDS:  # noqa: E721
            raise ValueError("unknown Stage 1 rule")
        if type(self.status) is not str or self.status not in ("pass", "fail", "indeterminate"):  # noqa: E721
            raise ValueError("unknown Stage 1 check status")
        if type(self.violation_codes) is not tuple or any(  # noqa: E721
            type(code) is not str or re.fullmatch(r"[a-z][a-z0-9_]*", code) is None
            for code in self.violation_codes
        ):
            raise ValueError("check violations must be typed stable codes")
        if tuple(sorted(set(self.violation_codes))) != self.violation_codes:
            raise ValueError("check violation codes must be sorted and unique")
        if (self.status == "pass") != (not self.violation_codes):
            raise ValueError("non-pass requires a reason; pass cannot carry violations")

    def to_mapping(self) -> dict[str, object]:
        return {
            "rule_id": self.rule_id, "status": self.status,
            "violation_codes": list(self.violation_codes),
        }

    @classmethod
    def from_mapping(cls, value: object) -> Stage1Check:
        if type(value) is not dict:  # noqa: E721
            raise ValueError("Stage 1 check must be a closed JSON object")
        mapping = cast(dict[str, object], value)
        if set(mapping) != {"rule_id", "status", "violation_codes"} or type(mapping["violation_codes"]) is not list:
            raise ValueError("Stage 1 check has missing or unknown fields")
        # Constructor validates exact primitive types; casts do not grant trust.
        return cls(cast(str, mapping["rule_id"]), cast(str, mapping["status"]),
                   tuple(cast(list[str], mapping["violation_codes"])))
