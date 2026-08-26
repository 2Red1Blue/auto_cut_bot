"""Window-local continuity discrepancies, not evidence or Admission authority.

The caller supplies the exact Store-read input closure. Shared draft validation
checks its value identities; neither public DTO construction nor this analysis
proves database commitment. Equal adjacent booleans do not prove matching events,
characters or states. An empty result is not a semantic acceptance decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..store.models import CommittedSemanticInputs, CommittedVlmSemanticInput
from .stage1_draft import Stage1DraftError, Stage1DraftPolicy, stage1_draft_prompt_inputs

_HASH = re.compile(r"sha256:[0-9a-f]{64}\Z")
_Direction = Literal["previous", "next"]
_Kind = Literal["conflict", "missing_context"]
_SourceKey = tuple[str, str, int, str]


class ContinuityAnalysisError(ValueError):
    """Invalid continuity values or ambiguous window adjacency."""


def _hash(value: object) -> str:
    if type(value) is not str or _HASH.fullmatch(value) is None:  # noqa: E721
        raise ContinuityAnalysisError("continuity references must be lowercase sha256")
    return value


def _closed(value: object, keys: set[str]) -> dict[str, object]:
    if type(value) is not dict:  # noqa: E721
        raise ContinuityAnalysisError("continuity value must be a closed object")
    raw = cast(dict[object, object], value)
    if any(type(key) is not str for key in raw) or set(raw) != keys:  # noqa: E721
        raise ContinuityAnalysisError("continuity object has missing or unknown fields")
    return cast(dict[str, object], raw)


def _array(value: object) -> tuple[object, ...]:
    if type(value) is not list:  # noqa: E721
        raise ContinuityAnalysisError("continuity mapping arrays must be lists")
    return tuple(cast(list[object], value))


def _hash_tuple(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple:  # noqa: E721
        raise ContinuityAnalysisError(f"{label} must be a tuple")
    hashes = tuple(_hash(item) for item in cast(tuple[object, ...], value))
    if hashes != tuple(sorted(set(hashes))):
        raise ContinuityAnalysisError(f"{label} must be sorted and unique")
    return hashes


@dataclass(frozen=True, slots=True)
class ContinuityClaim:
    window_manifest_sha256: str
    direction: _Direction
    continues: bool
    state_fact_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        _hash(self.window_manifest_sha256)
        if type(self.direction) is not str or self.direction not in ("previous", "next"):  # noqa: E721
            raise ContinuityAnalysisError("continuity direction must be previous or next")
        if type(self.continues) is not bool:  # noqa: E721
            raise ContinuityAnalysisError("continuity continues must be an exact boolean")
        facts = _hash_tuple(self.state_fact_ids, "continuity state_fact_ids")
        if bool(facts) != self.continues:
            raise ContinuityAnalysisError(
                "continuity state facts must match the raw continuation flag"
            )

    @classmethod
    def from_mapping(cls, value: object) -> ContinuityClaim:
        item = _closed(
            value, {"window_manifest_sha256", "direction", "continues", "state_fact_ids"}
        )
        return cls(
            _hash(item["window_manifest_sha256"]),
            cast(_Direction, item["direction"]),
            cast(bool, item["continues"]),
            tuple(_hash(value) for value in _array(item["state_fact_ids"])),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "window_manifest_sha256": self.window_manifest_sha256,
            "direction": self.direction,
            "continues": self.continues,
            "state_fact_ids": list(self.state_fact_ids),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ContinuityIssue:
    kind: _Kind
    windows: tuple[str, ...]
    claims: tuple[ContinuityClaim, ...]

    def __post_init__(self) -> None:
        if type(self.kind) is not str or self.kind not in ("conflict", "missing_context"):  # noqa: E721
            raise ContinuityAnalysisError("continuity issue kind is unsupported")
        windows = _hash_tuple(self.windows, "continuity issue windows")
        if type(self.claims) is not tuple or any(
            type(claim) is not ContinuityClaim for claim in self.claims
        ):  # noqa: E721
            raise ContinuityAnalysisError(
                "continuity claims must be a tuple of exact ContinuityClaim values"
            )
        keys = tuple(canonical_json_bytes(claim.to_mapping()) for claim in self.claims)
        if keys != tuple(sorted(set(keys))):
            raise ContinuityAnalysisError("continuity claims must be sorted and unique")
        if tuple(sorted(claim.window_manifest_sha256 for claim in self.claims)) != windows:
            raise ContinuityAnalysisError("continuity windows must exactly match the claims")
        if self.kind == "missing_context":
            if len(self.claims) != 1 or not self.claims[0].continues:
                raise ContinuityAnalysisError(
                    "missing context requires one true continuation claim"
                )
        elif (
            len(self.claims) != 2
            or {claim.direction for claim in self.claims} != {"previous", "next"}
            or self.claims[0].continues == self.claims[1].continues
        ):
            raise ContinuityAnalysisError(
                "conflict requires opposite flags at two adjacent window sides"
            )

    @classmethod
    def from_mapping(cls, value: object) -> ContinuityIssue:
        item = _closed(value, {"kind", "windows", "claims"})
        return cls(
            cast(_Kind, item["kind"]),
            tuple(_hash(value) for value in _array(item["windows"])),
            tuple(ContinuityClaim.from_mapping(value) for value in _array(item["claims"])),
        )

    def to_mapping(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "windows": list(self.windows),
            "claims": [claim.to_mapping() for claim in self.claims],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())

    @property
    def issue_id(self) -> str:
        """Stable content identity; omitted from the hashed payload itself."""
        return self.canonical_hash


def _claim(item: CommittedVlmSemanticInput, direction: _Direction) -> ContinuityClaim:
    continuity = item.semantic_pack.semantic_pack.continuity
    previous = direction == "previous"
    return ContinuityClaim(
        item.source_window.window_manifest_sha256,
        direction,
        continuity.continues_from_previous if previous else continuity.continues_into_next,
        continuity.entry_state_fact_refs if previous else continuity.exit_state_fact_refs,
    )


def _issue(kind: _Kind, *claims: ContinuityClaim) -> ContinuityIssue:
    return ContinuityIssue(
        kind,
        tuple(sorted(claim.window_manifest_sha256 for claim in claims)),
        tuple(sorted(claims, key=lambda claim: canonical_json_bytes(claim.to_mapping()))),
    )


def _source_key(item: CommittedVlmSemanticInput) -> _SourceKey:
    window = item.source_window
    return (window.source_id, window.source_sha256, window.stream_index, window.source_clock_id)


def analyze_continuity(
    inputs: CommittedSemanticInputs, *, policy: Stage1DraftPolicy
) -> tuple[ContinuityIssue, ...]:
    """Retain contradictions and missing adjacent context without interpreting facts.

    Adjacent means equal Source ID/hash, stream and clock, and exactly touching
    core ranges. There is no fallback across gaps or source boundaries. Two
    possible neighbors are rejected instead of silently choosing one.
    """
    try:
        stage1_draft_prompt_inputs(inputs, policy=policy)
    except Stage1DraftError as error:
        raise ContinuityAnalysisError("continuity input identity or policy is invalid") from error

    starts: dict[tuple[_SourceKey, int], list[CommittedVlmSemanticInput]] = {}
    ends: dict[tuple[_SourceKey, int], list[CommittedVlmSemanticInput]] = {}
    for item in inputs.inputs:
        group, window = _source_key(item), item.source_window
        starts.setdefault((group, window.core_start_pts), []).append(item)
        ends.setdefault((group, window.core_end_pts), []).append(item)

    issues: list[ContinuityIssue] = []
    for item in inputs.inputs:
        group, window = _source_key(item), item.source_window
        previous = ends.get((group, window.core_start_pts), [])
        following = starts.get((group, window.core_end_pts), [])
        if len(previous) > 1 or len(following) > 1:
            raise ContinuityAnalysisError("continuity window adjacency is ambiguous")
        previous_claim, next_claim = _claim(item, "previous"), _claim(item, "next")
        if not previous and previous_claim.continues:
            issues.append(_issue("missing_context", previous_claim))
        if not following:
            if next_claim.continues:
                issues.append(_issue("missing_context", next_claim))
        else:
            neighbor_claim = _claim(following[0], "previous")
            if next_claim.continues != neighbor_claim.continues:
                issues.append(_issue("conflict", next_claim, neighbor_claim))
    return tuple(sorted(issues, key=lambda issue: canonical_json_bytes(issue.to_mapping())))
