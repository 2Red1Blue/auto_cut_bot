"""Pure, deterministic Stage 1 semantic compiler.

This module deliberately projects semantic provenance only.  It has no media
materialization, physical timing, edit-mode, transcript, or cut authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..media.types import canonical_sha256
from ..source_manifest import SourcePurposeDeniedError
from ..store import CommittedSemanticInputs
from .authority import (
    AuditedStage1Draft,
    FrozenStage1Policy,
    InputDisposition,
    Stage1AuthorityError,
    require_committed_semantic_inputs,
    stage1_subject_sha256,
)
from .rules import evaluate_rules, indeterminate_rules

CoverageKind = Literal["fact", "event", "source_window", "compiler_obligation"]
CoverageStatus = Literal["resolved", "tainted", "unresolved", "conflicted"]
AdmissionDecision = Literal["admitted", "denied"]


class Stage1CompilationError(ValueError):
    """The closed Stage 1 projection or its coverage universe is invalid."""


def _sha(value: object, label: str) -> str:
    if type(value) is not str or not value.startswith("sha256:") or len(value) != 71:  # noqa: E721
        raise Stage1CompilationError(f"{label} must be a sha256 identity")
    return value


@dataclass(frozen=True, slots=True)
class EpisodeDigest:
    episode_index: int
    source_window_manifest_sha256: str
    summary: str

    def __post_init__(self) -> None:
        if type(self.episode_index) is not int or self.episode_index < 0:  # noqa: E721
            raise Stage1CompilationError("episode_index must be non-negative")
        _sha(self.source_window_manifest_sha256, "source_window_manifest_sha256")
        if type(self.summary) is not str or not self.summary.strip():  # noqa: E721
            raise Stage1CompilationError("episode digest summary must be non-empty")

    def to_mapping(self) -> dict[str, object]:
        return {"episode_index": self.episode_index, "source_window_manifest_sha256": self.source_window_manifest_sha256, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class EpisodeDigestSet:
    items: tuple[EpisodeDigest, ...]

    def __post_init__(self) -> None:
        items = tuple(self.items)
        if not items or any(type(item) is not EpisodeDigest for item in items):  # noqa: E721
            raise Stage1CompilationError("episode digests must be exact and non-empty")
        keys = tuple((item.episode_index, item.source_window_manifest_sha256) for item in items)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise Stage1CompilationError("episode digests must be sorted and unique")
        object.__setattr__(self, "items", items)

    def to_mapping(self) -> dict[str, object]:
        return {"items": [item.to_mapping() for item in self.items]}


@dataclass(frozen=True, slots=True)
class EventCard:
    event_id: str
    event_kind: str
    fact_ids: tuple[str, ...]
    source_window_manifest_sha256: str
    summary: str

    def __post_init__(self) -> None:
        if type(self.event_id) is not str or not self.event_id.startswith("sha256:"):  # noqa: E721
            raise Stage1CompilationError("event_id must be a semantic identity")
        if type(self.event_kind) is not str or not self.event_kind.strip():  # noqa: E721
            raise Stage1CompilationError("event_kind must be non-empty")
        facts = tuple(self.fact_ids)
        if not facts or any(type(item) is not str or not item.startswith("sha256:") for item in facts):
            raise Stage1CompilationError("event card fact_ids must be semantic identities")
        if facts != tuple(sorted(facts)) or len(facts) != len(set(facts)):
            raise Stage1CompilationError("event card fact_ids must be sorted and unique")
        _sha(self.source_window_manifest_sha256, "source_window_manifest_sha256")
        if type(self.summary) is not str or not self.summary.strip():  # noqa: E721
            raise Stage1CompilationError("event card summary must be non-empty")
        object.__setattr__(self, "fact_ids", facts)

    def to_mapping(self) -> dict[str, object]:
        return {"event_id": self.event_id, "event_kind": self.event_kind, "fact_ids": list(self.fact_ids), "source_window_manifest_sha256": self.source_window_manifest_sha256, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class EventCardSet:
    items: tuple[EventCard, ...]

    def __post_init__(self) -> None:
        items = tuple(self.items)
        keys = tuple((item.event_id, item.source_window_manifest_sha256) for item in items)
        if any(type(item) is not EventCard for item in items) or keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise Stage1CompilationError("event cards must be exact, sorted, and unique")
        object.__setattr__(self, "items", items)

    def to_mapping(self) -> dict[str, object]:
        return {"items": [item.to_mapping() for item in self.items]}


@dataclass(frozen=True, slots=True)
class NarrativeGraph:
    event_dependencies: tuple[tuple[str, tuple[str, ...]], ...]

    def __post_init__(self) -> None:
        entries = tuple(self.event_dependencies)
        keys = tuple(item[0] for item in entries)
        if keys != tuple(sorted(keys)) or len(keys) != len(set(keys)):
            raise Stage1CompilationError("narrative graph events must be sorted and unique")
        for event_id, dependencies in entries:
            if type(event_id) is not str or not event_id.startswith("sha256:") or tuple(dependencies) != tuple(sorted(dependencies)):
                raise Stage1CompilationError("narrative graph dependencies are invalid")
        object.__setattr__(self, "event_dependencies", entries)

    def to_mapping(self) -> dict[str, object]:
        return {"event_dependencies": [{"event_id": event_id, "depends_on_event_ids": list(dependencies)} for event_id, dependencies in self.event_dependencies]}


@dataclass(frozen=True, slots=True)
class CoverageRow:
    coverage_id: str
    kind: CoverageKind
    status: CoverageStatus

    def __post_init__(self) -> None:
        if type(self.coverage_id) is not str or not self.coverage_id.strip():  # noqa: E721
            raise Stage1CompilationError("coverage_id must be non-empty")
        if self.kind not in {"fact", "event", "source_window", "compiler_obligation"}:
            raise Stage1CompilationError("coverage kind is unsupported")
        if self.status not in {"resolved", "tainted", "unresolved", "conflicted"}:
            raise Stage1CompilationError("coverage status is unsupported")

    def to_mapping(self) -> dict[str, str]:
        return {"coverage_id": self.coverage_id, "kind": self.kind, "status": self.status}


@dataclass(frozen=True, slots=True)
class CoverageLedger:
    rows: tuple[CoverageRow, ...]

    def __post_init__(self) -> None:
        rows = tuple(self.rows)
        ids = tuple(item.coverage_id for item in rows)
        if not rows or any(type(item) is not CoverageRow for item in rows):  # noqa: E721
            raise Stage1CompilationError("coverage ledger rows must be exact and non-empty")
        if ids != tuple(sorted(ids)) or len(ids) != len(set(ids)):
            raise Stage1CompilationError("coverage ledger rows must be sorted and unique")
        object.__setattr__(self, "rows", rows)

    def to_mapping(self) -> dict[str, object]:
        return {"rows": [item.to_mapping() for item in self.rows]}


@dataclass(frozen=True, slots=True)
class EvidenceDiagnostics:
    non_resolved_window_manifest_sha256s: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {"non_resolved_window_manifest_sha256s": list(self.non_resolved_window_manifest_sha256s)}


@dataclass(frozen=True, slots=True)
class ConflictDiagnostics:
    conflicted_window_manifest_sha256s: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {"conflicted_window_manifest_sha256s": list(self.conflicted_window_manifest_sha256s)}


@dataclass(frozen=True, slots=True)
class DependencyClosureProof:
    event_fact_dependencies: tuple[tuple[str, tuple[str, ...]], ...]

    def to_mapping(self) -> dict[str, object]:
        return {"event_fact_dependencies": [{"event_id": event_id, "fact_ids": list(fact_ids)} for event_id, fact_ids in self.event_fact_dependencies]}


@dataclass(frozen=True, slots=True, init=False)
class CoverageAdmission:
    decision: AdmissionDecision
    subject_sha256: str
    _rule_results: tuple[object, ...]

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        raise TypeError("CoverageAdmission can only be constructed by Stage 1 evaluation")

    @classmethod
    def _from_evaluation(
        cls,
        decision: AdmissionDecision,
        subject_sha256: str,
        rule_results: tuple[object, ...],
    ) -> CoverageAdmission:
        result = object.__new__(cls)
        object.__setattr__(result, "decision", decision)
        object.__setattr__(result, "subject_sha256", subject_sha256)
        object.__setattr__(result, "_rule_results", rule_results)
        return result

    def to_mapping(self) -> dict[str, object]:
        return {"decision": self.decision, "rules": [item.to_mapping() for item in self._rule_results], "subject_sha256": self.subject_sha256}  # type: ignore[attr-defined]


@dataclass(frozen=True, slots=True)
class Stage1Compilation:
    episode_digests: EpisodeDigestSet
    event_cards: EventCardSet
    narrative_graph: NarrativeGraph
    coverage_ledger: CoverageLedger
    evidence_diagnostics: EvidenceDiagnostics
    conflict_diagnostics: ConflictDiagnostics
    dependency_closure_proof: DependencyClosureProof
    coverage_admission: CoverageAdmission

    def to_mapping(self) -> dict[str, object]:
        return {"conflict_diagnostics": self.conflict_diagnostics.to_mapping(), "coverage_admission": self.coverage_admission.to_mapping(), "coverage_ledger": self.coverage_ledger.to_mapping(), "dependency_closure_proof": self.dependency_closure_proof.to_mapping(), "episode_digests": self.episode_digests.to_mapping(), "event_cards": self.event_cards.to_mapping(), "evidence_diagnostics": self.evidence_diagnostics.to_mapping(), "narrative_graph": self.narrative_graph.to_mapping()}

    @property
    def canonical_hash(self) -> str:
        return canonical_sha256(self.to_mapping())


def compile_stage1(
    inputs: CommittedSemanticInputs,
    draft: AuditedStage1Draft,
    policy: FrozenStage1Policy,
) -> Stage1Compilation:
    """Compile a closed semantic authority tuple under strict-global admission."""

    inputs = require_committed_semantic_inputs(inputs)
    if type(draft) is not AuditedStage1Draft or type(policy) is not FrozenStage1Policy:  # noqa: E721
        raise Stage1AuthorityError("Stage 1 requires exact draft and frozen policy")
    windows = tuple(item.source_window.window_manifest_sha256 for item in inputs.inputs)
    dispositions: dict[str, InputDisposition] = {
        item.window_manifest_sha256: item.status for item in draft.input_dispositions
    }
    if set(dispositions) != set(windows):
        raise Stage1CompilationError("audited dispositions must cover exactly the committed windows")
    subject = stage1_subject_sha256(inputs, draft, policy)
    initial_rules = indeterminate_rules(subject)
    if any(result.status != "indeterminate" for result in initial_rules):
        raise Stage1CompilationError("Stage 1 rules must begin indeterminate")
    try:
        inputs.source_grant.require_purpose("semantic_analysis")
        authorized = True
    except SourcePurposeDeniedError:
        authorized = False
    rows: list[CoverageRow] = []
    digests: list[EpisodeDigest] = []
    cards: list[EventCard] = []
    graph: list[tuple[str, tuple[str, ...]]] = []
    proof: list[tuple[str, tuple[str, ...]]] = []
    for item in inputs.inputs:
        window = item.source_window.window_manifest_sha256
        status = dispositions[window]
        pack = item.semantic_pack.semantic_pack
        digests.append(EpisodeDigest(item.source_window.episode_index, window, pack.window_summary.summary))
        rows.append(CoverageRow(f"source_window:{window}", "source_window", status))
        for fact in pack.facts:
            rows.append(CoverageRow(f"fact:{window}:{fact.fact_id}", "fact", status))
        for event in pack.events:
            cards.append(EventCard(event.event_id, event.event_kind.value, event.fact_refs, window, event.summary))
            rows.append(CoverageRow(f"event:{window}:{event.event_id}", "event", status))
            graph.append((event.event_id, tuple(sorted(event.cause_event_refs))))
            proof.append((event.event_id, event.fact_refs))
    for obligation in draft.compiler_obligations:
        rows.append(CoverageRow(f"compiler_obligation:{obligation.obligation_id}", "compiler_obligation", "resolved"))
    ledger = CoverageLedger(tuple(sorted(rows, key=lambda row: row.coverage_id)))
    expected_count = sum(1 + len(item.semantic_pack.semantic_pack.facts) + len(item.semantic_pack.semantic_pack.events) for item in inputs.inputs) + len(draft.compiler_obligations)
    coverage_complete = len(ledger.rows) == expected_count
    statuses = tuple(dispositions[window] for window in windows)
    resolved = all(status == "resolved" for status in statuses)
    conflict_free = all(status != "conflicted" for status in statuses)
    rules = evaluate_rules(subject, authorized=authorized, resolved=resolved, conflict_free=conflict_free, coverage_complete=coverage_complete)
    decision: AdmissionDecision = "admitted" if all(rule.status == "pass" for rule in rules) else "denied"
    non_resolved = tuple(sorted(window for window, status in dispositions.items() if status != "resolved"))
    conflicts = tuple(sorted(window for window, status in dispositions.items() if status == "conflicted"))
    return Stage1Compilation(
        EpisodeDigestSet(tuple(sorted(digests, key=lambda item: (item.episode_index, item.source_window_manifest_sha256)))),
        EventCardSet(tuple(sorted(cards, key=lambda item: (item.event_id, item.source_window_manifest_sha256)))),
        NarrativeGraph(tuple(sorted(graph))),
        ledger,
        EvidenceDiagnostics(non_resolved),
        ConflictDiagnostics(conflicts),
        DependencyClosureProof(tuple(sorted(proof))),
        CoverageAdmission._from_evaluation(  # pyright: ignore[reportPrivateUsage]
            decision, subject, rules
        ),
    )
