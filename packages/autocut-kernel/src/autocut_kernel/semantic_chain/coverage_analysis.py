"""Stage 1 observation coverage, before Ledger/proof/Admission persistence.

Every input fact, event, window and generated obligation is retained. This
analysis is not a committed artifact or an admission token. It deliberately
does not interpret narrative success criteria as fulfilled or infer identity
merges from names. The upper compiler owns diagnostics, Ledger seeds and the
independent KC evaluator.

Resolution describes direct observation evidence and assignment, not transitive
taint isolation. An Event can be directly resolved while depending on another
unresolved Event. The dependency projector/proof must propagate those causes;
consumers must never treat a resolved row as a claim of an untainted closure.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from ..contracts.compiler.canonical import canonical_json_hash
from ..store.models import CommittedSemanticInputs
from .continuity_analysis import analyze_continuity
from .diagnostic_models import ConfidenceMeasurement
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import Confidence
from .stage1_draft import Stage1DraftPolicy, decode_stage1_draft


@dataclass(frozen=True, slots=True)
class Stage1CoveragePolicy:
    """Explicit first strategy; no uncalibrated confidence default."""

    minimum_confidence: str
    coverage_mode: str

    def __post_init__(self) -> None:
        Confidence(self.minimum_confidence, "rule")
        if type(self.coverage_mode) is not str or self.coverage_mode != "strict_global":  # noqa: E721
            raise ValueError("only the implemented strict_global coverage strategy is allowed")

    def to_mapping(self) -> dict[str, object]:
        return {"minimum_confidence": self.minimum_confidence, "coverage_mode": self.coverage_mode}

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


@dataclass(frozen=True, slots=True)
class ObservationCoverage:
    """Raw input-owned unit; the Ledger compiler later resolves canonical owners.

    Obligations have no raw media owner: their unit ID belongs to the decoded
    draft, whose hash is bound by CoverageAnalysis. Other unit IDs are exact
    input observation/window IDs, never display names.
    """

    unit_type: str
    unit_id: str
    window_manifest_sha256: str | None
    resolution_status: str
    disposition: str
    assignment_ids: tuple[str, ...]
    evidence_refs: tuple[SemanticObjectRef, ...]
    reason_codes: tuple[str, ...]
    cause_ids: tuple[str, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "unit_type": self.unit_type, "unit_id": self.unit_id,
            "window_manifest_sha256": self.window_manifest_sha256,
            "resolution_status": self.resolution_status, "disposition": self.disposition,
            "assignment_ids": list(self.assignment_ids),
            "evidence_refs": [ref.to_mapping() for ref in self.evidence_refs],
            "reason_codes": list(self.reason_codes), "cause_ids": list(self.cause_ids),
        }


@dataclass(frozen=True, slots=True)
class CoverageAnalysis:
    input_binding_sha256: str
    draft_sha256: str
    coverage_policy_sha256: str
    rows: tuple[ObservationCoverage, ...]
    low_confidence_causes: tuple[ConfidenceMeasurement, ...]

    def to_mapping(self) -> dict[str, object]:
        return {
            "input_binding_sha256": self.input_binding_sha256,
            "draft_sha256": self.draft_sha256,
            "coverage_policy_sha256": self.coverage_policy_sha256,
            "rows": [row.to_mapping() for row in self.rows],
            "low_confidence_causes": [cause.to_mapping() for cause in self.low_confidence_causes],
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.to_mapping())


def coverage_reason_id(draft_sha256: str, reason_code: str, unit_type: str, unit_id: str) -> str:
    """Stable origin of a missing assignment/summary, not a safety result."""
    if reason_code not in ("unassigned", "summary_evidence_missing"):
        raise ValueError("coverage reason has no local-unit cause encoding")
    return canonical_json_hash({
        "draft_sha256": draft_sha256, "reason_code": reason_code,
        "unit_type": unit_type, "unit_id": unit_id,
    })


def analyze_observation_coverage(
    inputs: CommittedSemanticInputs,
    raw_draft: bytes,
    *,
    draft_policy: Stage1DraftPolicy,
    coverage_policy: Stage1CoveragePolicy,
) -> CoverageAnalysis:
    """Re-decode raw draft and derive every row from source observations.

    The Store reader, not this pure function, proves committed provenance.
    Returned assignment IDs are local draft IDs or exact window-summary IDs;
    they are not Graph references. The subsequent compiler must resolve them.
    No row is excluded, no missing Event is fabricated, and no output pass flag
    is accepted. Source authorization is checked independently of disposition.
    """
    if type(coverage_policy) is not Stage1CoveragePolicy:  # noqa: E721
        raise ValueError("coverage requires an explicit Stage1CoveragePolicy")
    draft = decode_stage1_draft(raw_draft, inputs=inputs, policy=draft_policy)
    inputs.source_grant.require_purpose("semantic_analysis")
    authorized = {(item.source_id, item.content_sha256) for item in inputs.source_grant.sources}
    if any((item.source_window.source_id, item.source_window.source_sha256) not in authorized
           for item in inputs.inputs):
        raise ValueError("analyzed Source is not in the exact semantic_analysis grant")
    threshold = Decimal(coverage_policy.minimum_confidence)
    continuity = analyze_continuity(inputs, policy=draft_policy)
    by_window = {item.source_window.window_manifest_sha256: item for item in inputs.inputs}
    source = inputs.source_manifest.reference
    source_owner = SemanticMemberIdentity(
        source.artifact_type, source.logical_id, source.revision, source.scope, source.content_hash
    )

    def raw_ref(window: str, kind: str, object_id: str) -> SemanticObjectRef:
        owner = by_window[window].semantic_pack.reference
        identity = SemanticMemberIdentity(
            owner.artifact_type, owner.logical_id, owner.revision, owner.scope, owner.content_hash
        )
        return SemanticObjectRef(identity, f"vlm_{kind}", object_id)

    # Assignment is explicit: a Beat or an assigned requirement uses the Fact,
    # or the VLM summary itself cites it. Simply appearing in Graph is not use.
    event_assignments: dict[str, set[str]] = {}
    obligation_assignments: dict[str, set[str]] = {}
    fact_assignments: dict[str, set[str]] = {}
    for beat in draft.beats:
        for ref in beat.event_refs:
            event_assignments.setdefault(ref.object_id, set()).add(f"beat:{beat.beat_id}")
        for obligation_id in beat.obligation_ids:
            obligation_assignments.setdefault(obligation_id, set()).add(f"beat:{beat.beat_id}")
    for thread in draft.story_threads:
        for obligation_id in thread.obligation_ids:
            obligation_assignments.setdefault(obligation_id, set()).add(f"thread:{thread.story_thread_id}")
    for obligation in draft.obligations:
        if obligation.obligation_id in obligation_assignments:
            for ref in obligation.required_fact_refs:
                fact_assignments.setdefault(ref.object_id, set()).add(f"obligation:{obligation.obligation_id}")

    merge_causes: dict[str, set[str]] = {}
    for proposal in draft.merge_proposals:
        cause = canonical_json_hash(proposal.to_mapping())
        for ref in proposal.entity_refs:
            merge_causes.setdefault(ref.object_id, set()).add(cause)

    rows: list[ObservationCoverage] = []
    fact_rows: dict[str, ObservationCoverage] = {}
    measurements: dict[str, ConfidenceMeasurement] = {}

    def low_confidence(ref: SemanticObjectRef, kind: str, value: Decimal) -> set[str]:
        if value >= threshold:
            return set()
        measurement = ConfidenceMeasurement(
            ref, kind, Confidence.from_decimal(value, method="source").value,
            coverage_policy.minimum_confidence, coverage_policy.canonical_hash,
        )
        cause_id = measurement.canonical_hash
        measurements[cause_id] = measurement
        return {cause_id}

    def make_row(
        kind: str, unit_id: str, window: str | None, disposition: str,
        assignments: set[str], evidence: tuple[SemanticObjectRef, ...],
        reasons: set[str], causes: set[str],
    ) -> ObservationCoverage:
        if disposition == "unassigned":
            reasons.add("unassigned")
            causes.add(coverage_reason_id(draft.canonical_hash, "unassigned", kind, unit_id))
        conflict = "continuity_conflict" in reasons
        status = "conflicted" if conflict else "unresolved" if reasons else "resolved"
        if status == "unresolved":
            disposition = "unassigned"
        return ObservationCoverage(
            kind, unit_id, window, status, disposition, tuple(sorted(assignments)),
            tuple(sorted(set(evidence), key=lambda ref: ref.canonical_hash)),
            tuple(sorted(reasons)), tuple(sorted(causes)),
        )

    for item in inputs.inputs:
        pack = item.semantic_pack.semantic_pack
        window = item.source_window.window_manifest_sha256
        summary_id = f"summary:{window}"
        summary_ok = pack.window_summary.confidence >= threshold
        summary_causes = low_confidence(
            SemanticObjectRef(source_owner, "source_window", window),
            "window_summary", pack.window_summary.confidence,
        )
        entity_confidence_causes = {
            entity.entity_id: low_confidence(raw_ref(window, "entity", entity.entity_id),
                                              "entity", entity.support.confidence)
            for entity in pack.entities
        }
        event_facts: dict[str, set[str]] = {}
        supporting_facts: set[str] = set(pack.window_summary.fact_refs)
        for event in pack.events:
            if event.event_id in event_assignments:
                for fact_id in event.fact_refs:
                    event_facts.setdefault(fact_id, set()).update(event_assignments[event.event_id])
            if event.event_id in pack.window_summary.event_refs:
                supporting_facts.update(event.fact_refs)

        window_reasons: set[str] = set()
        window_causes: set[str] = set()
        for issue in continuity:
            if window in issue.windows:
                window_reasons.add(f"continuity_{issue.kind}")
                window_causes.add(issue.canonical_hash)
        start = len(rows)
        for fact in pack.facts:
            assignments = fact_assignments.get(fact.fact_id, set()) | event_facts.get(fact.fact_id, set())
            disposition = "narrative" if assignments else "supporting" if fact.fact_id in supporting_facts else "unassigned"
            if disposition == "supporting":
                assignments = {summary_id}
            entity_ids = (fact.subject_ref,) + ((fact.object_ref,) if fact.object_ref else ())
            reasons = set(window_reasons)
            causes = set(window_causes)
            low_causes = low_confidence(raw_ref(window, "fact", fact.fact_id), "fact", fact.support.confidence)
            for entity_id in entity_ids:
                low_causes.update(entity_confidence_causes[entity_id])
            if disposition == "supporting":
                low_causes.update(summary_causes)
            if low_causes:
                reasons.add("low_confidence")
                causes.update(low_causes)
            for entity_id in entity_ids:
                if entity_id in merge_causes:
                    reasons.add("identity_unresolved")
                    causes.update(merge_causes[entity_id])
            row = make_row("fact", fact.fact_id, window, disposition, assignments,
                           (raw_ref(window, "fact", fact.fact_id),), reasons, causes)
            rows.append(row)
            fact_rows[fact.fact_id] = row

        for event in pack.events:
            assignments = event_assignments.get(event.event_id, set())
            disposition = "narrative" if assignments else "supporting" if event.event_id in pack.window_summary.event_refs else "unassigned"
            if disposition == "supporting":
                assignments = {summary_id}
            reasons, causes = set(window_reasons), set(window_causes)
            low_causes = low_confidence(raw_ref(window, "event", event.event_id), "event", event.support.confidence)
            for entity_id in event.participant_refs:
                low_causes.update(entity_confidence_causes[entity_id])
            if disposition == "supporting":
                low_causes.update(summary_causes)
            if low_causes:
                reasons.add("low_confidence")
                causes.update(low_causes)
            for entity_id in event.participant_refs:
                if entity_id in merge_causes:
                    reasons.add("identity_unresolved")
                    causes.update(merge_causes[entity_id])
            for fact_id in event.fact_refs:
                fact_row = fact_rows[fact_id]
                reasons.update(fact_row.reason_codes)
                causes.update(fact_row.cause_ids)
            rows.append(make_row("event", event.event_id, window, disposition, assignments,
                                 (raw_ref(window, "event", event.event_id),), reasons, causes))

        contents = rows[start:]
        for row in contents:
            window_reasons.update(row.reason_codes)
            window_causes.update(row.cause_ids)
        # A window reference records where a summary was observed, not why its
        # claims are supported. Even if every Fact is assigned by the draft,
        # an ungrounded summary remains an explicit missing-evidence condition.
        if not pack.window_summary.fact_refs and not pack.window_summary.event_refs:
            window_reasons.add("summary_evidence_missing")
            window_causes.add(coverage_reason_id(draft.canonical_hash, "summary_evidence_missing", "source_window", window))
        # Entities that occur in neither a Fact nor an Event still cannot hide
        # unresolved identity or low-confidence evidence from window coverage.
        for entity in pack.entities:
            if entity.support.confidence < threshold:
                window_reasons.add("low_confidence")
                window_causes.update(entity_confidence_causes[entity.entity_id])
            if entity.entity_id in merge_causes:
                window_reasons.add("identity_unresolved")
                window_causes.update(merge_causes[entity.entity_id])
        if not summary_ok:
            window_reasons.add("low_confidence")
            window_causes.update(summary_causes)
        disposition = "narrative" if any(row.disposition == "narrative" for row in contents) else "supporting"
        rows.append(make_row(
            "source_window", window, window, disposition, {summary_id},
            (SemanticObjectRef(source_owner, "source_window", window),),
            window_reasons, window_causes,
        ))

    for obligation in draft.obligations:
        assignments = obligation_assignments.get(obligation.obligation_id, set())
        reasons: set[str] = set()
        causes: set[str] = set()
        for ref in obligation.required_fact_refs:
            reasons.update(fact_rows[ref.object_id].reason_codes)
            causes.update(fact_rows[ref.object_id].cause_ids)
        rows.append(make_row(
            "obligation", obligation.obligation_id, None,
            "narrative" if assignments else "unassigned", assignments,
            tuple(raw_ref(ref.window_manifest_sha256, "fact", ref.object_id)
                  for ref in obligation.required_fact_refs), reasons, causes,
        ))
    return CoverageAnalysis(
        draft.input_binding_sha256, draft.canonical_hash, coverage_policy.canonical_hash,
        tuple(sorted(rows, key=lambda row: (row.unit_type, row.unit_id))),
        tuple(measurements[key] for key in sorted(measurements)),
    )
