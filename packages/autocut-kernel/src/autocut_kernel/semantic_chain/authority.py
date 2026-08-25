"""Closed authority values for the pure Stage 1 semantic compiler."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from ..media.types import canonical_sha256
from ..store import CommittedSemanticInputs

_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_OBLIGATION = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]*\Z")
InputDisposition = Literal["resolved", "tainted", "unresolved", "conflicted"]


class Stage1AuthorityError(ValueError):
    """A caller supplied a non-closed or non-audited Stage 1 authority value."""


def _sha256(value: object, label: str) -> str:
    if type(value) is not str or not _SHA256.fullmatch(value):  # noqa: E721
        raise Stage1AuthorityError(f"{label} must be a lowercase sha256 digest")
    return value


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise Stage1AuthorityError(f"{label} must be non-empty text")
    return value


@dataclass(frozen=True, slots=True)
class FrozenStage1Policy:
    """Identity of the frozen policy that governs one Stage 1 projection."""

    policy_id: str
    policy_sha256: str

    def __post_init__(self) -> None:
        _text(self.policy_id, "policy_id")
        _sha256(self.policy_sha256, "policy_sha256")

    def to_mapping(self) -> dict[str, str]:
        return {"policy_id": self.policy_id, "policy_sha256": self.policy_sha256}


@dataclass(frozen=True, slots=True)
class AuditedInputDisposition:
    """Audited semantic usability of exactly one committed source window."""

    window_manifest_sha256: str
    status: InputDisposition

    def __post_init__(self) -> None:
        _sha256(self.window_manifest_sha256, "window_manifest_sha256")
        if self.status not in {"resolved", "tainted", "unresolved", "conflicted"}:
            raise Stage1AuthorityError("input disposition status is unsupported")

    def to_mapping(self) -> dict[str, str]:
        return {
            "status": self.status,
            "window_manifest_sha256": self.window_manifest_sha256,
        }


@dataclass(frozen=True, slots=True)
class CompilerObligation:
    """One non-physical compiler obligation that must appear in coverage."""

    obligation_id: str

    def __post_init__(self) -> None:
        if type(self.obligation_id) is not str or not _OBLIGATION.fullmatch(self.obligation_id):  # noqa: E721
            raise Stage1AuthorityError("obligation_id must be an opaque identifier")

    def to_mapping(self) -> dict[str, str]:
        return {"obligation_id": self.obligation_id}


@dataclass(frozen=True, slots=True)
class AuditedStage1Draft:
    """Closed, audited compiler input; it cannot carry synthesized rule results."""

    draft_sha256: str
    input_dispositions: tuple[AuditedInputDisposition, ...]
    compiler_obligations: tuple[CompilerObligation, ...]

    def __post_init__(self) -> None:
        _sha256(self.draft_sha256, "draft_sha256")
        dispositions = tuple(self.input_dispositions)
        obligations = tuple(self.compiler_obligations)
        if not dispositions or any(type(item) is not AuditedInputDisposition for item in dispositions):  # noqa: E721
            raise Stage1AuthorityError("input_dispositions must contain exact audited dispositions")
        if not obligations or any(type(item) is not CompilerObligation for item in obligations):  # noqa: E721
            raise Stage1AuthorityError("compiler_obligations must contain exact obligations")
        disposition_ids = tuple(item.window_manifest_sha256 for item in dispositions)
        obligation_ids = tuple(item.obligation_id for item in obligations)
        if disposition_ids != tuple(sorted(disposition_ids)) or len(disposition_ids) != len(set(disposition_ids)):
            raise Stage1AuthorityError("input_dispositions must be canonically sorted and unique")
        if obligation_ids != tuple(sorted(obligation_ids)) or len(obligation_ids) != len(set(obligation_ids)):
            raise Stage1AuthorityError("compiler_obligations must be canonically sorted and unique")
        object.__setattr__(self, "input_dispositions", dispositions)
        object.__setattr__(self, "compiler_obligations", obligations)

    def to_mapping(self) -> dict[str, object]:
        return {
            "compiler_obligations": [item.to_mapping() for item in self.compiler_obligations],
            "draft_sha256": self.draft_sha256,
            "input_dispositions": [item.to_mapping() for item in self.input_dispositions],
        }


def require_committed_semantic_inputs(value: object) -> CommittedSemanticInputs:
    """Reject paths, mappings, raw VLM payloads, and all compatibility wrappers."""

    if type(value) is not CommittedSemanticInputs:  # noqa: E721
        raise Stage1AuthorityError("Stage 1 requires exact CommittedSemanticInputs")
    return value


def stage1_subject_sha256(
    inputs: CommittedSemanticInputs,
    draft: AuditedStage1Draft,
    policy: FrozenStage1Policy,
) -> str:
    """Stable identity of the authority tuple being evaluated."""

    return canonical_sha256(
        {
            "draft": draft.to_mapping(),
            "input_windows": [
                item.source_window.window_manifest_sha256 for item in inputs.inputs
            ],
            "policy": policy.to_mapping(),
            "source_manifest": inputs.source_manifest.canonical_hash,
        }
    )
