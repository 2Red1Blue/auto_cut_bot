"""Closed, semantic-only Stage 1 projection and strict-global admission."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..media.types import canonical_sha256
from ..source_manifest import SourcePurposeDeniedError
from ..store import CommittedSemanticInputs
from .authority import (
    Stage1AuthorityError,
    _DecodedStage1Authority,  # pyright: ignore[reportPrivateUsage]
    require_committed_semantic_inputs,
    require_sha256,
)
from .rules import evaluate_rules, indeterminate_rules

_CoverageKind = Literal["fact", "event", "source_window", "obligation"]
_CoverageStatus = Literal["resolved", "tainted", "unresolved", "conflicted"]


class Stage1CompilationError(ValueError):
    """A closed Stage 1 semantic business member is invalid."""


def _text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():  # noqa: E721
        raise Stage1CompilationError(f"{label} must be non-empty text")
    return value


def _ordered_unique(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    if values != tuple(sorted(values)) or len(values) != len(set(values)):
        raise Stage1CompilationError(f"{label} must be sorted and unique")
    for value in values:
        require_sha256(value, label)
    return values


@dataclass(frozen=True, slots=True)
class _EpisodeDigest:
    episode_index: int
    source_window_manifest_sha256: str
    summary: str
    entity_ids: tuple[str, ...]
    continuity: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise Stage1CompilationError("episode_index must be non-negative")
        require_sha256(self.source_window_manifest_sha256, "source window")
        _text(self.summary, "episode summary")
        _ordered_unique(self.entity_ids, "entity_ids")
        if self.continuity != tuple(sorted(self.continuity)) or len(self.continuity) != len(set(self.continuity)):
            raise Stage1CompilationError("continuity must be sorted and unique")

    def to_mapping(self) -> dict[str, object]:
        return {"continuity": list(self.continuity), "entity_ids": list(self.entity_ids), "episode_index": self.episode_index, "source_window_manifest_sha256": self.source_window_manifest_sha256, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class _EventCard:
    event_id: str
    event_kind: str
    fact_ids: tuple[str, ...]
    source_window_manifest_sha256: str
    summary: str

    def __post_init__(self) -> None:
        require_sha256(self.event_id, "event_id")
        _text(self.event_kind, "event_kind")
        if not self.fact_ids:
            raise Stage1CompilationError("event card fact_ids must not be empty")
        _ordered_unique(self.fact_ids, "event card fact_ids")
        require_sha256(self.source_window_manifest_sha256, "source window")
        _text(self.summary, "event summary")

    def to_mapping(self) -> dict[str, object]:
        return {"event_id": self.event_id, "event_kind": self.event_kind, "fact_ids": list(self.fact_ids), "source_window_manifest_sha256": self.source_window_manifest_sha256, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class _CoverageRow:
    coverage_id: str
    kind: _CoverageKind
    status: _CoverageStatus

    def __post_init__(self) -> None:
        _text(self.coverage_id, "coverage_id")
        if self.kind not in {"fact", "event", "source_window", "obligation"}:
            raise Stage1CompilationError("coverage kind is unsupported")
        if self.status not in {"resolved", "tainted", "unresolved", "conflicted"}:
            raise Stage1CompilationError("coverage status is unsupported")

    def to_mapping(self) -> dict[str, str]:
        return {"coverage_id": self.coverage_id, "kind": self.kind, "status": self.status}


@dataclass(frozen=True, slots=True)
class _ClosedMember:
    mapping: dict[str, object]

    def to_mapping(self) -> dict[str, object]:
        return self.mapping


@dataclass(frozen=True, slots=True)
class Stage1Compilation:
    """Read-only result; the admission witness remains private to this module."""

    _business_members: tuple[_ClosedMember, ...]
    _admission: _ClosedMember

    def to_mapping(self) -> dict[str, object]:
        return {
            "conflict_diagnostics": self._business_members[5].to_mapping(),
            "coverage_admission": self._admission.to_mapping(),
            "coverage_ledger": self._business_members[3].to_mapping(),
            "dependency_closure_proof": self._business_members[6].to_mapping(),
            "episode_digests": self._business_members[0].to_mapping(),
            "event_cards": self._business_members[1].to_mapping(),
            "evidence_diagnostics": self._business_members[4].to_mapping(),
            "narrative_graph": self._business_members[2].to_mapping(),
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())

    @property
    def decision(self) -> Literal["admitted", "denied"]:
        return self._admission.mapping["decision"]  # type: ignore[return-value]


def _expected_coverage(inputs: CommittedSemanticInputs) -> tuple[tuple[str, _CoverageKind], ...]:
    rows: list[tuple[str, _CoverageKind]] = []
    for item in inputs.inputs:
        window = item.source_window.window_manifest_sha256
        pack = item.semantic_pack.semantic_pack
        rows.append((f"source_window:{window}", "source_window"))
        rows.extend((f"fact:{window}:{fact.fact_id}", "fact") for fact in pack.facts)
        rows.extend((f"event:{window}:{event.event_id}", "event") for event in pack.events)
    rows.extend((f"obligation:{item}", "obligation") for item in ("cross_window_merge", "semantic_closure"))
    expected = tuple(sorted(rows))
    if len(expected) != len(set(expected)):
        raise Stage1CompilationError("expected coverage universe contains duplicate identities")
    return expected


def compile_stage1(
    inputs: CommittedSemanticInputs, authority: _DecodedStage1Authority
) -> Stage1Compilation:
    """Compile exact committed semantics with no caller-provided draft authority."""

    inputs = require_committed_semantic_inputs(inputs)
    if type(authority) is not _DecodedStage1Authority:  # noqa: E721
        raise Stage1AuthorityError("Stage 1 requires an exact decoder-created authority")
    authority._assert_bound(inputs)  # pyright: ignore[reportPrivateUsage]
    try:
        inputs.source_grant.require_purpose("semantic_analysis")
        authorized = True
    except SourcePurposeDeniedError:
        authorized = False
    window_states: dict[str, _CoverageStatus] = {
        item.subject_id: item.status for item in authority.window_states
    }
    obligation_states: dict[str, _CoverageStatus] = {
        item.subject_id: item.status
        for item in authority.obligation_states
    }
    rows: list[_CoverageRow] = []
    digests: list[_EpisodeDigest] = []
    cards: list[_EventCard] = []
    graph: list[dict[str, object]] = []
    proof: list[dict[str, object]] = []
    for item in inputs.inputs:
        window = item.source_window.window_manifest_sha256
        pack = item.semantic_pack.semantic_pack
        continuity = tuple(sorted(name for name, enabled in (("continues_from_previous", pack.continuity.continues_from_previous), ("continues_into_next", pack.continuity.continues_into_next)) if enabled))
        digests.append(_EpisodeDigest(item.source_window.episode_index, window, pack.window_summary.summary, tuple(sorted(entity.entity_id for entity in pack.entities)), continuity))
        rows.append(_CoverageRow(f"source_window:{window}", "source_window", window_states[window]))
        rows.extend(_CoverageRow(f"fact:{window}:{fact.fact_id}", "fact", window_states[window]) for fact in pack.facts)
        for event in pack.events:
            cards.append(_EventCard(event.event_id, event.event_kind.value, event.fact_refs, window, event.summary))
            rows.append(_CoverageRow(f"event:{window}:{event.event_id}", "event", window_states[window]))
            graph.append({"depends_on_event_ids": list(_ordered_unique(event.cause_event_refs, "event dependencies")), "event_id": event.event_id})
            proof.append({"event_id": event.event_id, "fact_ids": list(event.fact_refs)})
    rows.extend(_CoverageRow(f"obligation:{item}", "obligation", obligation_states[item]) for item in ("cross_window_merge", "semantic_closure"))
    ordered_rows = tuple(sorted(rows, key=lambda item: item.coverage_id))
    expected = _expected_coverage(inputs)
    actual = tuple((item.coverage_id, item.kind) for item in ordered_rows)
    coverage_complete = actual == expected and len(ordered_rows) == len(set(actual))
    all_resolved = coverage_complete and all(row.status == "resolved" for row in ordered_rows)
    taint_subjects = tuple(sorted(item.subject_id for item in authority.taint_seeds))
    conflict_subjects = tuple(sorted(item.subject_id for item in authority.conflict_claims))
    conflict_free = not conflict_subjects and all(row.status != "conflicted" for row in ordered_rows)
    resolved = all_resolved and not taint_subjects
    precommit = (
        _ClosedMember({"items": [item.to_mapping() for item in sorted(digests, key=lambda value: (value.episode_index, value.source_window_manifest_sha256))]}),
        _ClosedMember({"items": [item.to_mapping() for item in sorted(cards, key=lambda value: (value.event_id, value.source_window_manifest_sha256))]}),
        _ClosedMember({"event_dependencies": sorted(graph, key=lambda value: str(value["event_id"]))}),
        _ClosedMember({"rows": [item.to_mapping() for item in ordered_rows]}),
        _ClosedMember({"taint_seeds": [{"seed_sha256": item.seed_sha256, "subject_id": item.subject_id} for item in authority.taint_seeds], "unresolved_subject_ids": sorted({row.coverage_id for row in ordered_rows if row.status != "resolved"})}),
        _ClosedMember({"competing_claims": [{"competing_claim_sha256": item.competing_claim_sha256, "subject_id": item.subject_id} for item in authority.conflict_claims], "possible_duplicate_entity_ids": []}),
        _ClosedMember({"event_fact_dependencies": sorted(proof, key=lambda value: str(value["event_id"]))}),
    )
    subject = canonical_sha256({"authority": authority._to_mapping(), "business_members": [member.to_mapping() for member in precommit]})  # pyright: ignore[reportPrivateUsage]
    initial = indeterminate_rules(subject)
    if any(item.status != "indeterminate" for item in initial):
        raise Stage1CompilationError("rule lifecycle must begin indeterminate")
    rules = evaluate_rules(subject, authorized=authorized, resolved=resolved, conflict_free=conflict_free, coverage_complete=coverage_complete)
    decision: Literal["admitted", "denied"] = "admitted" if all(item.status == "pass" for item in rules) else "denied"
    admission = _ClosedMember({"decision": decision, "rules": [item.to_mapping() for item in rules], "subject_sha256": subject})
    return Stage1Compilation(precommit, admission)
