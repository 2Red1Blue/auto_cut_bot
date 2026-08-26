"""Compile factual members, actual diagnostic causes and the Stage 1 Ledger.

This pure compiler returns pending business members, not an Admission or proof
of database commitment. The Command must read the exact committed input closure
and supply its audited raw draft. Dependency proof and KC evaluation follow it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from ..contracts.compiler.canonical import canonical_json_bytes, canonical_json_hash
from ..store.models import ArtifactMember, ArtifactScope, CommittedSemanticInputs
from .continuity_analysis import ContinuityClaim, analyze_continuity
from .coverage_analysis import (
    CoverageAnalysis,
    ObservationCoverage,
    Stage1CoveragePolicy,
    analyze_observation_coverage,
    coverage_reason_id,
)
from .diagnostic_models import (
    ConflictClaim,
    ConflictDiagnostic,
    ConflictDiagnostics,
    ContinuityClaimValue,
    EvidenceDiagnostic,
    EvidenceDiagnostics,
    IdentityObservationValue,
    MergeProposalCause,
)
from .ledger_models import (
    CoverageCounts,
    CoverageLedger,
    CoverageRow,
    CoverageWindow,
    LocalCoverageWindowRef,
    TaintSeed,
)
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import EpisodeDigestSet
from .narrative_projection import NarrativeProjection, draft_node_id, project_narrative
from .stage1_draft import Stage1Draft, Stage1DraftPolicy, decode_stage1_draft


class CoverageCompilationError(ValueError):
    """A coverage cause or assignment cannot resolve to its actual owner."""


@dataclass(frozen=True, slots=True)
class Stage1CoverageCompilation:
    narrative: NarrativeProjection
    evidence_diagnostics: ArtifactMember
    conflict_diagnostics: ArtifactMember
    coverage_ledger: ArtifactMember

    @property
    def members(self) -> tuple[ArtifactMember, ...]:
        return (
            self.narrative.event_cards, self.narrative.episode_digests,
            self.narrative.narrative_graph, self.evidence_diagnostics,
            self.conflict_diagnostics, self.coverage_ledger,
        )


def _refs(refs: tuple[SemanticObjectRef, ...]) -> tuple[SemanticObjectRef, ...]:
    return tuple(sorted(set(refs), key=lambda ref: canonical_json_bytes(ref.to_mapping())))


def _id(binding: str, kind: str, origin: str) -> str:
    return canonical_json_hash({
        "schema_version": "stage1-coverage-id-v1", "input_binding_sha256": binding,
        "kind": kind, "origin": origin,
    })


class _Compiler:
    """Per-call indexes; none escapes as an authority token or mutable output."""

    def __init__(
        self, inputs: CommittedSemanticInputs, draft: Stage1Draft,
        analysis: CoverageAnalysis, narrative: NarrativeProjection,
        *, revision: int, scope: ArtifactScope,
    ) -> None:
        self.inputs, self.draft, self.analysis = inputs, draft, analysis
        self.scope, self.revision = scope, revision
        source = inputs.source_manifest.reference
        self.source = SemanticMemberIdentity(
            source.artifact_type, source.logical_id, source.revision, source.scope, source.content_hash
        )
        self.graph = SemanticMemberIdentity.from_artifact_member(narrative.narrative_graph)
        self.cards = SemanticMemberIdentity.from_artifact_member(narrative.event_cards)
        digest_owner = SemanticMemberIdentity.from_artifact_member(narrative.episode_digests)
        digests = EpisodeDigestSet.from_mapping(json.loads(narrative.episode_digests.payload_json))
        self.digest_by_window = {
            ref.object_id: SemanticObjectRef(digest_owner, "episode_digest", digest.episode_id)
            for digest in digests.digests for ref in digest.source_window_refs
        }
        self.by_window = {item.source_window.window_manifest_sha256: item for item in inputs.inputs}
        self.pack_by_window = {
            window: SemanticMemberIdentity(
                item.semantic_pack.reference.artifact_type, item.semantic_pack.reference.logical_id,
                item.semantic_pack.reference.revision, item.semantic_pack.reference.scope,
                item.semantic_pack.reference.content_hash,
            )
            for window, item in self.by_window.items()
        }
        self.affected_by_cause: dict[str, tuple[SemanticObjectRef, ...]] = {}
        for row in analysis.rows:
            for cause in row.cause_ids:
                self.affected_by_cause[cause] = _refs((
                    *self.affected_by_cause.get(cause, ()), self.unit_ref(row),
                ))

    def member(self, kind: str, payload: dict[str, object]) -> ArtifactMember:
        raw = canonical_json_bytes(payload)
        return ArtifactMember(
            kind, kind, self.revision, self.scope, canonical_json_hash(payload), raw.decode("utf-8")
        )

    def identity(self, kind: str, origin: str = "") -> str:
        return _id(self.draft.input_binding_sha256, kind, origin)

    def raw_ref(self, window: str, kind: str, object_id: str) -> SemanticObjectRef:
        return SemanticObjectRef(self.pack_by_window[window], f"vlm_{kind}", object_id)

    def window_ref(self, window: str) -> SemanticObjectRef:
        return SemanticObjectRef(self.source, "source_window", window)

    def unit_ref(self, row: ObservationCoverage) -> SemanticObjectRef:
        if row.unit_type == "source_window":
            return self.window_ref(row.unit_id)
        if row.unit_type == "event":
            return SemanticObjectRef(self.cards, "event", row.unit_id)
        if row.unit_type == "obligation":
            return SemanticObjectRef(self.graph, "obligation", draft_node_id(self.draft, "obligation", row.unit_id))
        if row.unit_type == "fact":
            return SemanticObjectRef(self.graph, "fact", row.unit_id)
        raise CoverageCompilationError("unknown coverage unit")

    def canonical_observation(self, raw: SemanticObjectRef) -> SemanticObjectRef:
        if raw.object_type == "source_window":
            return raw
        if raw.object_type == "vlm_event":
            return SemanticObjectRef(self.cards, "event", raw.object_id)
        if raw.object_type in ("vlm_fact", "vlm_entity"):
            return SemanticObjectRef(self.graph, raw.object_type.removeprefix("vlm_"), raw.object_id)
        raise CoverageCompilationError("unknown diagnostic observation")

    def claim(self, claim: ContinuityClaim) -> ContinuityClaimValue:
        window = claim.window_manifest_sha256
        return ContinuityClaimValue(
            self.window_ref(window), self.pack_by_window[window], claim.direction,
            claim.continues, tuple(self.raw_ref(window, "fact", key) for key in claim.state_fact_ids),
        )

    def diagnostics(
        self, raw: bytes, draft_policy: Stage1DraftPolicy,
    ) -> tuple[ArtifactMember, ArtifactMember, dict[str, EvidenceDiagnostic | ConflictDiagnostic]]:
        causes: dict[str, EvidenceDiagnostic | ConflictDiagnostic] = {}
        claims: list[ConflictClaim] = []
        merges: list[MergeProposalCause] = []

        def add(cause_id: str, item: EvidenceDiagnostic | ConflictDiagnostic) -> None:
            if cause_id in causes:
                raise CoverageCompilationError("ambiguous diagnostic cause")
            causes[cause_id] = item

        for measurement in self.analysis.low_confidence_causes:
            cause = measurement.canonical_hash
            origin = self.canonical_observation(measurement.observation_ref)
            add(cause, EvidenceDiagnostic(
                self.identity("low_confidence", cause), "low_confidence", origin,
                (measurement.observation_ref,),
                _refs((*self.affected_by_cause[cause], origin)), measurement, None, None,
            ))

        for proposal in self.draft.merge_proposals:
            cause = canonical_json_hash(proposal.to_mapping())
            observations = tuple(self.raw_ref(ref.window_manifest_sha256, "entity", ref.object_id)
                                 for ref in proposal.entity_refs)
            evidence = tuple(self.raw_ref(ref.window_manifest_sha256, ref.object_type, ref.object_id)
                             for ref in proposal.evidence_refs)
            merges.append(MergeProposalCause(cause, proposal.merge_id, proposal.rationale, observations, evidence))
            local_claims = tuple(ConflictClaim(
                self.identity("merge_claim", cause + ref.canonical_hash), IdentityObservationValue(ref),
            ) for ref in observations)
            claims.extend(local_claims)
            origins = tuple(self.canonical_observation(ref) for ref in observations)
            add(cause, ConflictDiagnostic(
                self.identity("identity_unresolved", cause), "possible_duplicate",
                origins[0], _refs((*observations, *evidence)),
                _refs((*self.affected_by_cause[cause], *origins)), cause,
                tuple(claim.claim_id for claim in local_claims),
            ))

        for issue in analyze_continuity(self.inputs, policy=draft_policy):
            cause = issue.canonical_hash
            values = tuple(self.claim(claim) for claim in issue.claims)
            evidence = _refs(tuple(ref for value in values
                                   for ref in (value.source_window_ref, *value.state_fact_refs)))
            if issue.kind == "missing_context":
                value = values[0]
                add(cause, EvidenceDiagnostic(
                    self.identity("continuity_missing_context", cause), "continuity_missing_context",
                    value.source_window_ref, evidence, self.affected_by_cause[cause], None, value, None,
                ))
            else:
                local_claims = tuple(ConflictClaim(
                    self.identity("continuity_claim", cause + value.canonical_hash), value,
                ) for value in values)
                claims.extend(local_claims)
                add(cause, ConflictDiagnostic(
                    self.identity("continuity_conflict", cause), "timeline_order_conflict",
                    values[0].source_window_ref, evidence, self.affected_by_cause[cause],
                    cause, tuple(claim.claim_id for claim in local_claims),
                ))

        for row in self.analysis.rows:
            # Inherited `unassigned` reasons do not invent another origin.
            if not row.assignment_ids:
                cause = coverage_reason_id(self.draft.canonical_hash, "unassigned", row.unit_type, row.unit_id)
                add(cause, EvidenceDiagnostic(
                    self.identity("unassigned", cause), "unassigned", self.unit_ref(row),
                    row.evidence_refs, self.affected_by_cause[cause], None, None, None,
                ))
            if row.unit_type == "source_window":
                summary = self.by_window[row.unit_id].semantic_pack.semantic_pack.window_summary
                if not summary.fact_refs and not summary.event_refs:
                    cause = coverage_reason_id(self.draft.canonical_hash, "summary_evidence_missing", "source_window", row.unit_id)
                    add(cause, EvidenceDiagnostic(
                        self.identity("summary_evidence_missing", cause), "summary_evidence_missing",
                        self.unit_ref(row), row.evidence_refs, self.affected_by_cause[cause],
                        None, None, summary.summary,
                    ))

        if causes.keys() != self.affected_by_cause.keys():
            raise CoverageCompilationError("diagnostic causes do not exactly cover analyzed failures")
        raw_hash = "sha256:" + hashlib.sha256(raw).hexdigest()
        evidence_set = EvidenceDiagnostics(
            self.identity("evidence_diagnostics"), self.draft.input_binding_sha256,
            raw_hash, self.draft.canonical_hash,
            tuple(item for item in causes.values() if isinstance(item, EvidenceDiagnostic)),
        )
        conflict_set = ConflictDiagnostics(
            self.identity("conflict_diagnostics"), self.draft.input_binding_sha256,
            raw_hash, self.draft.canonical_hash,
            tuple(item for item in causes.values() if isinstance(item, ConflictDiagnostic)),
            tuple(claims), tuple(merges),
        )
        return (self.member("evidence_diagnostics", evidence_set.to_mapping()),
                self.member("conflict_diagnostics", conflict_set.to_mapping()), causes)

    def assignment(self, value: str) -> SemanticObjectRef:
        kind, separator, local_id = value.partition(":")
        if not separator:
            raise CoverageCompilationError("assignment has no typed local identity")
        if kind == "summary" and local_id in self.digest_by_window:
            return self.digest_by_window[local_id]
        if kind in ("beat", "thread", "obligation"):
            node_type = "story_thread" if kind == "thread" else kind
            return SemanticObjectRef(self.graph, node_type, draft_node_id(self.draft, node_type, local_id))
        raise CoverageCompilationError("assignment cannot resolve to a projected member")

    def ledger(
        self, evidence: ArtifactMember, conflicts: ArtifactMember,
        causes: dict[str, EvidenceDiagnostic | ConflictDiagnostic],
    ) -> ArtifactMember:
        evidence_owner = SemanticMemberIdentity.from_artifact_member(evidence)
        conflict_owner = SemanticMemberIdentity.from_artifact_member(conflicts)
        windows: list[CoverageWindow] = []
        for window, item in self.by_window.items():
            pack = item.semantic_pack.semantic_pack
            windows.append(CoverageWindow(
                window, self.window_ref(window), SemanticObjectRef(self.source, "source", item.source_window.source_id),
                tuple(SemanticObjectRef(self.graph, "fact", fact.fact_id) for fact in pack.facts),
                tuple(SemanticObjectRef(self.cards, "event", event.event_id) for event in pack.events),
            ))
        rows: list[CoverageRow] = []
        seeds: list[TaintSeed] = []
        for row in self.analysis.rows:
            unit = self.unit_ref(row)
            target = LocalCoverageWindowRef(row.unit_id) if row.unit_type == "source_window" else unit
            diagnostics = tuple(causes[cause] for cause in row.cause_ids)
            diagnostic_refs = tuple(SemanticObjectRef(
                evidence_owner if isinstance(item, EvidenceDiagnostic) else conflict_owner,
                "diagnostic", item.diagnostic_id,
            ) for item in diagnostics)
            seed_id: str | None = None
            if row.resolution_status != "resolved":
                seed_id = self.identity("taint_seed", row.unit_type + ":" + row.unit_id)
                affected = _refs((unit, *(ref for item in diagnostics for ref in item.affected_refs)))
                roots = tuple(ref for ref in affected if ref.object_type != "source_window")
                window_ids = tuple(ref.object_id for ref in affected if ref.object_type == "source_window")
                unknown = bool(set(row.reason_codes) & {"identity_unresolved", "continuity_missing_context"})
                seeds.append(TaintSeed(
                    seed_id, roots, window_ids, roots if unknown else (), window_ids if unknown else (), row.reason_codes,
                ))
            rows.append(CoverageRow(
                self.identity("coverage_row", row.unit_type + ":" + row.unit_id), row.unit_type,
                target, row.resolution_status, row.disposition,
                _refs(tuple(self.assignment(value) for value in row.assignment_ids)),
                row.evidence_refs, _refs(diagnostic_refs), seed_id,
            ))
        counts = CoverageCounts(
            sum(len(item.semantic_pack.semantic_pack.facts) for item in self.inputs.inputs),
            sum(len(item.semantic_pack.semantic_pack.events) for item in self.inputs.inputs),
            len(self.inputs.inputs), len(self.draft.obligations),
        )
        ledger = CoverageLedger(
            self.identity("coverage_ledger"), self.draft.input_binding_sha256, self.draft.canonical_hash,
            self.analysis.coverage_policy_sha256, tuple(windows), tuple(rows), tuple(seeds), counts,
        )
        return self.member("coverage_ledger", ledger.to_mapping())


def compile_stage1_coverage(
    inputs: CommittedSemanticInputs, raw_draft: bytes, *, draft_policy: Stage1DraftPolicy,
    coverage_policy: Stage1CoveragePolicy, scope: ArtifactScope, revision: int,
) -> Stage1CoverageCompilation:
    """Produce six pending members; the real eight-member Command is separate."""
    draft = decode_stage1_draft(raw_draft, inputs=inputs, policy=draft_policy)
    analysis = analyze_observation_coverage(
        inputs, raw_draft, draft_policy=draft_policy, coverage_policy=coverage_policy,
    )
    narrative = project_narrative(inputs, draft, scope=scope, revision=revision)
    compiler = _Compiler(inputs, draft, analysis, narrative, revision=revision, scope=scope)
    evidence, conflicts, causes = compiler.diagnostics(raw_draft, draft_policy)
    ledger = compiler.ledger(evidence, conflicts, causes)
    return Stage1CoverageCompilation(narrative, evidence, conflicts, ledger)
