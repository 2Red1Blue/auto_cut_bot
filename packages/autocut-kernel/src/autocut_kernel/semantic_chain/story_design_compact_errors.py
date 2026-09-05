"""Bounded compact-wire diagnostics, without model text or private identities."""

from __future__ import annotations

import re

from .story_design_draft import StoryDesignDraftError

_CODES = frozenset({
    "COMPACT_REFERENCE_TYPE_MISMATCH", "COMPACT_REFERENCE_NOT_FOUND", "COMPACT_DUPLICATE_REFERENCE",
    "COMPACT_SOURCE_SELECTION_INVALID", "COMPACT_MATERIAL_INFEASIBLE", "COMPACT_EDITING_PROFILE_NOT_FOUND",
    "COMPACT_FIELD_INVALID", "COMPACT_JSON_INVALID", "COMPACT_BUDGET_EXCEEDED", "COMPACT_SCHEMA_UNSUPPORTED",
})
_PATH = re.compile(
    r"\$(?:\.(?:schema_version|proposals|title|narrative_claim|thread_refs|obligation_refs|"
    r"key_subject_refs|genre_tags|editing_profile_ref|target_duration_seconds|teaser_strategy|audience_hook|"
    r"material_requirements|obligation_ref|minimum_usable_seconds|additional_checks|source_constraints|"
    r"source_selection|allowed_source_refs|forbidden_source_refs)(?:\[[0-9]+\])?)*\Z"
)


class CompactDraftError(StoryDesignDraftError):
    def __init__(self, error_code: str, *, json_path: str = "$", proposal_index: int | None = None) -> None:
        self.error_code = error_code if type(error_code) is str and error_code in _CODES else "COMPACT_FIELD_INVALID"
        self.json_path = json_path if type(json_path) is str and len(json_path) <= 256 and _PATH.fullmatch(json_path) else "$"
        self.proposal_index = proposal_index if type(proposal_index) is int and 0 <= proposal_index < 2**53 else None
        super().__init__(self.error_code)

    def to_diagnostic(self) -> dict[str, object]:
        # Reconstruct because exception attributes, unlike domain DTOs, are mutable.
        safe = CompactDraftError(self.error_code, json_path=self.json_path, proposal_index=self.proposal_index)
        return {"rule_id": None, "error_code": safe.error_code, "json_path": safe.json_path,
                "proposal_index": safe.proposal_index, "expected_object_type": None,
                "actual_object_type": None, "missing_count": None, "unexpected_count": None}
