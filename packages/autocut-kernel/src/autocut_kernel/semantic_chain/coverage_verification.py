"""Independent direct-coverage checks over raw inputs and six pending members.

No producer coverage/compiler/projector is invoked. Assignment relations and
causes are reconstructed from the decoded draft and actual raw observations.
This checks neither Store commitment nor transitive dependency isolation.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from decimal import Decimal

from ..contracts.compiler.canonical import canonical_json_hash
from ..store.models import ArtifactMember, CommittedSemanticInputs
from .continuity_analysis import analyze_continuity
from .coverage_analysis import Stage1CoveragePolicy
from .diagnostic_models import (
    ConfidenceMeasurement,
    ConflictClaim,
    ContinuityClaimValue,
    IdentityObservationValue,
    MergeProposalCause,
)
from .ledger_models import LocalCoverageWindowRef
from .member_refs import SemanticMemberIdentity, SemanticObjectRef
from .narrative_models import Confidence
from .stage1_checks import Stage1Check
from .stage1_draft import Stage1Draft, Stage1DraftPolicy, decode_stage1_draft
from .stage1_members import Stage1CoverageValues, decode_coverage_members

_RULES = (
    "KC-COV-001",
    "KC-COV-002",
    "KC-COV-003",
    "KC-COV-004",
    "KC-COV-005",
    "KC-EXCLUDE-001",
    "KC-GATE-001",
)
_Key = tuple[str, str]


@dataclass
class _Cause:
    reason: str
    origin: SemanticObjectRef
    evidence: frozenset[SemanticObjectRef]
    extra_origins: frozenset[SemanticObjectRef] = frozenset()
    measurement: ConfidenceMeasurement | None = None
    continuity: ContinuityClaimValue | None = None
    summary: str | None = None
    claims: tuple[ConflictClaim, ...] = ()
    merge: MergeProposalCause | None = None


@dataclass
class _Unit:
    reference: SemanticObjectRef
    assignments: set[SemanticObjectRef]
    evidence: frozenset[SemanticObjectRef]
    disposition: str
    causes: set[str] = field(default_factory=set)


def _draft_id(binding: str, kind: str, local_id: str) -> str:
    # The versioned identity recipe locates a node; it does not validate content.
    return canonical_json_hash(
        {
            "schema_version": "stage1-narrative-projection-id-v1",
            "input_binding_sha256": binding,
            "kind": kind,
            "local_id": local_id,
        }
    )


def _diagnostic_id(binding: str, kind: str, origin: str) -> str:
    return canonical_json_hash(
        {
            "schema_version": "stage1-coverage-id-v1",
            "input_binding_sha256": binding,
            "kind": kind,
            "origin": origin,
        }
    )


class _Truth:
    """Independent per-call relation indexes; no pending payload is an oracle."""

    def __init__(
        self,
        inputs: CommittedSemanticInputs,
        draft: Stage1Draft,
        values: Stage1CoverageValues,
        policy: Stage1CoveragePolicy,
    ) -> None:
        self.inputs, self.draft, self.values, self.policy = inputs, draft, values, policy
        source = inputs.source_manifest.reference
        self.source = SemanticMemberIdentity(
            source.artifact_type,
            source.logical_id,
            source.revision,
            source.scope,
            source.content_hash,
        )
        self.packs = {
            item.source_window.window_manifest_sha256: SemanticMemberIdentity(
                item.semantic_pack.reference.artifact_type,
                item.semantic_pack.reference.logical_id,
                item.semantic_pack.reference.revision,
                item.semantic_pack.reference.scope,
                item.semantic_pack.reference.content_hash,
            )
            for item in inputs.inputs
        }
        self.units: dict[_Key, _Unit] = {}
        self.causes: dict[str, _Cause] = {}
        self.entity_causes: dict[str, set[str]] = {}
        self.window_causes: dict[str, set[str]] = {window: set() for window in self.packs}
        self.low: dict[SemanticObjectRef, set[str]] = {}

    def canonical(self, kind: str, key: str) -> SemanticObjectRef:
        owner = "event_card_set" if kind == "event" else "narrative_graph"
        if kind == "source_window":
            return SemanticObjectRef(self.source, kind, key)
        if kind in ("obligation", "beat", "story_thread"):
            key = _draft_id(self.draft.input_binding_sha256, kind, key)
        return SemanticObjectRef(self.values.identity(owner), kind, key)

    def raw(self, window: str, kind: str, key: str) -> SemanticObjectRef:
        return SemanticObjectRef(self.packs[window], f"vlm_{kind}", key)

    def summary_assignment(self, episode_index: int) -> SemanticObjectRef:
        return SemanticObjectRef(
            self.values.identity("episode_digest_set"),
            "episode_digest",
            f"episode-{episode_index + 1}",
        )

    def local_cause(self, reason: str, key: _Key) -> str:
        return canonical_json_hash(
            {
                "draft_sha256": self.draft.canonical_hash,
                "reason_code": reason,
                "unit_type": key[0],
                "unit_id": key[1],
            }
        )

    def add_unit(
        self,
        key: _Key,
        assignments: set[SemanticObjectRef],
        evidence: frozenset[SemanticObjectRef],
        disposition: str,
        causes: set[str],
    ) -> None:
        if key in self.units:
            raise ValueError("raw coverage units must be unique")
        reference = self.canonical(*key)
        causes = set(causes)
        if not assignments:
            cause = self.local_cause("unassigned", key)
            self.causes[cause] = _Cause("unassigned", reference, evidence)
            causes.add(cause)
        self.units[key] = _Unit(reference, set(assignments), evidence, disposition, causes)

    def observations(self) -> None:
        threshold = Decimal(self.policy.minimum_confidence)
        seen: set[tuple[str, str]] = set()
        for item in self.inputs.inputs:
            window, pack = (
                item.source_window.window_manifest_sha256,
                item.semantic_pack.semantic_pack,
            )
            observations: list[tuple[str, str, Decimal]] = [
                ("entity", value.entity_id, value.support.confidence) for value in pack.entities
            ]
            observations.extend(
                ("fact", value.fact_id, value.support.confidence) for value in pack.facts
            )
            observations.extend(
                ("event", value.event_id, value.support.confidence) for value in pack.events
            )
            for kind, object_id, confidence in (
                *observations,
                ("window_summary", window, pack.window_summary.confidence),
            ):
                if (kind, object_id) in seen:
                    raise ValueError("raw observation identity is duplicated")
                seen.add((kind, object_id))
                ref = (
                    self.canonical("source_window", window)
                    if kind == "window_summary"
                    else self.raw(window, kind, object_id)
                )
                self.low[ref] = set()
                if confidence < threshold:
                    measurement = ConfidenceMeasurement(
                        ref,
                        kind,
                        Confidence.from_decimal(confidence, method="source").value,
                        self.policy.minimum_confidence,
                        self.policy.canonical_hash,
                    )
                    cause = measurement.canonical_hash
                    origin = self.canonical(
                        "source_window" if kind == "window_summary" else kind, object_id
                    )
                    self.causes[cause] = _Cause(
                        "low_confidence",
                        origin,
                        frozenset((ref,)),
                        frozenset((origin,)),
                        measurement=measurement,
                    )
                    self.low[ref].add(cause)

    def unknowns(self, draft_policy: Stage1DraftPolicy) -> None:
        binding = self.draft.input_binding_sha256
        for proposal in self.draft.merge_proposals:
            cause = canonical_json_hash(proposal.to_mapping())
            entities = tuple(
                self.raw(ref.window_manifest_sha256, "entity", ref.object_id)
                for ref in proposal.entity_refs
            )
            evidence = tuple(
                self.raw(ref.window_manifest_sha256, ref.object_type, ref.object_id)
                for ref in proposal.evidence_refs
            )
            claims = tuple(
                ConflictClaim(
                    _diagnostic_id(binding, "merge_claim", cause + ref.canonical_hash),
                    IdentityObservationValue(ref),
                )
                for ref in entities
            )
            origins = tuple(self.canonical("entity", ref.object_id) for ref in entities)
            self.causes[cause] = _Cause(
                "identity_unresolved",
                origins[0],
                frozenset((*entities, *evidence)),
                frozenset(origins),
                claims=claims,
                merge=MergeProposalCause(
                    cause, proposal.merge_id, proposal.rationale, entities, evidence
                ),
            )
            for ref in entities:
                self.entity_causes.setdefault(ref.object_id, set()).add(cause)
        for issue in analyze_continuity(self.inputs, policy=draft_policy):
            cause = issue.canonical_hash
            claims = tuple(
                ContinuityClaimValue(
                    self.canonical("source_window", claim.window_manifest_sha256),
                    self.packs[claim.window_manifest_sha256],
                    claim.direction,
                    claim.continues,
                    tuple(
                        self.raw(claim.window_manifest_sha256, "fact", key)
                        for key in claim.state_fact_ids
                    ),
                )
                for claim in issue.claims
            )
            evidence = frozenset(
                ref for claim in claims for ref in (claim.source_window_ref, *claim.state_fact_refs)
            )
            self.causes[cause] = _Cause(
                "continuity_" + issue.kind,
                claims[0].source_window_ref,
                evidence,
                continuity=claims[0] if issue.kind == "missing_context" else None,
                claims=tuple(
                    ConflictClaim(
                        _diagnostic_id(binding, "continuity_claim", cause + claim.canonical_hash),
                        claim,
                    )
                    for claim in claims
                )
                if issue.kind == "conflict"
                else (),
            )
            for window in issue.windows:
                self.window_causes[window].add(cause)

    def assignments(self) -> None:
        obligation_users: dict[str, set[SemanticObjectRef]] = {}
        event_users: dict[str, set[SemanticObjectRef]] = {}
        fact_users: dict[str, set[SemanticObjectRef]] = {}
        for beat in self.draft.beats:
            user = self.canonical("beat", beat.beat_id)
            for ref in beat.event_refs:
                event_users.setdefault(ref.object_id, set()).add(user)
            for key in beat.obligation_ids:
                obligation_users.setdefault(key, set()).add(user)
        for thread in self.draft.story_threads:
            for key in thread.obligation_ids:
                obligation_users.setdefault(key, set()).add(
                    self.canonical("story_thread", thread.story_thread_id)
                )
        for obligation in self.draft.obligations:
            if obligation_users.get(obligation.obligation_id):
                for ref in obligation.required_fact_refs:
                    fact_users.setdefault(ref.object_id, set()).add(
                        self.canonical("obligation", obligation.obligation_id)
                    )

        for item in self.inputs.inputs:
            window, pack = (
                item.source_window.window_manifest_sha256,
                item.semantic_pack.semantic_pack,
            )
            summary_ref = self.canonical("source_window", window)
            summary_assignment = self.summary_assignment(item.source_window.episode_index)
            background_facts = set(pack.window_summary.fact_refs)
            for event in pack.events:
                for fact_id in event.fact_refs:
                    fact_users.setdefault(fact_id, set()).update(
                        event_users.get(event.event_id, set())
                    )
                    if event.event_id in pack.window_summary.event_refs:
                        background_facts.add(fact_id)
            window_unit_keys: list[_Key] = []
            for fact in pack.facts:
                key = ("fact", fact.fact_id)
                users = set(fact_users.get(fact.fact_id, set()))
                disposition = (
                    "narrative"
                    if users
                    else "supporting"
                    if fact.fact_id in background_facts
                    else "unassigned"
                )
                ref = self.raw(window, "fact", fact.fact_id)
                causes = self.window_causes[window] | self.low[ref]
                for entity_id in (
                    fact.subject_ref,
                    *((fact.object_ref,) if fact.object_ref else ()),
                ):
                    causes |= self.low[
                        self.raw(window, "entity", entity_id)
                    ] | self.entity_causes.get(entity_id, set())
                if disposition == "supporting":
                    users.add(summary_assignment)
                    causes |= self.low[summary_ref]
                self.add_unit(key, users, frozenset((ref,)), disposition, causes)
                window_unit_keys.append(key)
            for event in pack.events:
                key = ("event", event.event_id)
                users = set(event_users.get(event.event_id, set()))
                disposition = (
                    "narrative"
                    if users
                    else "supporting"
                    if event.event_id in pack.window_summary.event_refs
                    else "unassigned"
                )
                ref = self.raw(window, "event", event.event_id)
                causes = self.window_causes[window] | self.low[ref]
                for entity_id in event.participant_refs:
                    causes |= self.low[
                        self.raw(window, "entity", entity_id)
                    ] | self.entity_causes.get(entity_id, set())
                for fact_id in event.fact_refs:
                    causes |= self.units[("fact", fact_id)].causes
                if disposition == "supporting":
                    users.add(summary_assignment)
                    causes |= self.low[summary_ref]
                self.add_unit(key, users, frozenset((ref,)), disposition, causes)
                window_unit_keys.append(key)
            causes = set(self.window_causes[window]) | self.low[summary_ref]
            for key in window_unit_keys:
                causes |= self.units[key].causes
            for entity in pack.entities:
                causes |= self.low[
                    self.raw(window, "entity", entity.entity_id)
                ] | self.entity_causes.get(entity.entity_id, set())
            if not pack.window_summary.fact_refs and not pack.window_summary.event_refs:
                cause = self.local_cause("summary_evidence_missing", ("source_window", window))
                self.causes[cause] = _Cause(
                    "summary_evidence_missing",
                    summary_ref,
                    frozenset((summary_ref,)),
                    summary=pack.window_summary.summary,
                )
                causes.add(cause)
            narrative_window = any(
                self.units[key].disposition == "narrative"
                and self.status(self.units[key]) != "unresolved"
                for key in window_unit_keys
            )
            self.add_unit(
                ("source_window", window),
                {summary_assignment},
                frozenset((summary_ref,)),
                "narrative" if narrative_window else "supporting",
                causes,
            )

        for obligation in self.draft.obligations:
            users = set(obligation_users.get(obligation.obligation_id, set()))
            causes: set[str] = set()
            for ref in obligation.required_fact_refs:
                causes.update(self.units[("fact", ref.object_id)].causes)
            evidence = frozenset(
                self.raw(ref.window_manifest_sha256, "fact", ref.object_id)
                for ref in obligation.required_fact_refs
            )
            self.add_unit(
                ("obligation", obligation.obligation_id),
                users,
                evidence,
                "narrative" if users else "unassigned",
                causes,
            )

    def status(self, unit: _Unit) -> str:
        if any(self.causes[key].reason == "continuity_conflict" for key in unit.causes):
            return "conflicted"
        return "unresolved" if unit.causes else "resolved"

    def affected(self) -> dict[str, frozenset[SemanticObjectRef]]:
        return {
            key: cause.extra_origins
            | frozenset(unit.reference for unit in self.units.values() if key in unit.causes)
            for key, cause in self.causes.items()
        }


def _check_universes(truth: _Truth, violations: dict[str, set[str]]) -> None:
    values, units, inputs = truth.values, truth.units, truth.inputs
    ledger = values.coverage_ledger
    for kind in ("fact", "event", "obligation"):
        expected = {
            unit.reference for (unit_kind, _key), unit in units.items() if unit_kind == kind
        }
        actual = {row.unit_ref for row in ledger.rows if row.unit_type == kind}
        if expected != actual or getattr(ledger.input_counts, kind) != len(expected):
            violations["KC-COV-001"].add("unit_conservation_mismatch")
        if kind != "event":
            nodes = {
                SemanticObjectRef(values.identity("narrative_graph"), kind, node.node_id)
                for node in values.narrative_graph.nodes
                if node.node_type == kind
            }
        else:
            nodes = {
                SemanticObjectRef(values.identity("event_card_set"), "event", card.event_id)
                for card in values.event_cards.events
            }
        if nodes != expected:
            violations["KC-COV-001"].add("canonical_unit_owner_mismatch")
    windows = {window.source_window_ref: window for window in ledger.windows}
    expected_windows = {truth.canonical("source_window", key) for key in truth.packs}
    if set(windows) != expected_windows or ledger.input_counts.source_window != len(
        expected_windows
    ):
        violations["KC-COV-002"].add("window_conservation_mismatch")
    for item in inputs.inputs:
        key, pack = item.source_window.window_manifest_sha256, item.semantic_pack.semantic_pack
        window = windows.get(truth.canonical("source_window", key))
        if window is None:
            continue
        if (
            window.source_ref
            != SemanticObjectRef(truth.source, "source", item.source_window.source_id)
            or set(window.fact_refs)
            != {truth.canonical("fact", fact.fact_id) for fact in pack.facts}
            or set(window.event_refs)
            != {truth.canonical("event", event.event_id) for event in pack.events}
        ):
            violations["KC-COV-002"].add("window_membership_mismatch")
    expected_digests: dict[str, set[SemanticObjectRef]] = {}
    for item in inputs.inputs:
        expected_digests.setdefault(f"episode-{item.source_window.episode_index + 1}", set()).add(
            truth.canonical("source_window", item.source_window.window_manifest_sha256)
        )
    actual_digests = {
        digest.episode_id: set(digest.source_window_refs)
        for digest in values.episode_digests.digests
    }
    if actual_digests != expected_digests:
        violations["KC-COV-002"].add("summary_assignment_owner_mismatch")


def _check_diagnostics(
    truth: _Truth,
    raw_draft: bytes,
    violations: dict[str, set[str]],
) -> tuple[dict[str, SemanticObjectRef], dict[str, frozenset[SemanticObjectRef]]]:
    values, draft = truth.values, truth.draft
    evidence, conflicts = values.evidence_diagnostics, values.conflict_diagnostics
    raw_hash = "sha256:" + hashlib.sha256(raw_draft).hexdigest()
    for model, rule in ((evidence, "KC-COV-003"), (conflicts, "KC-COV-004")):
        if (
            model.input_binding_sha256 != draft.input_binding_sha256
            or model.raw_draft_sha256 != raw_hash
            or model.canonical_draft_sha256 != draft.canonical_hash
        ):
            violations[rule].add("diagnostic_input_binding_mismatch")
    affected = truth.affected()
    expected_refs: dict[str, SemanticObjectRef] = {}
    expected_evidence: set[str] = set()
    expected_conflicts: set[str] = set()
    expected_claims: dict[str, ConflictClaim] = {}
    expected_merges: dict[str, MergeProposalCause] = {}
    evidence_by_id = {item.diagnostic_id: item for item in evidence.items}
    conflicts_by_id = {item.diagnostic_id: item for item in conflicts.items}
    for key, cause in truth.causes.items():
        diagnostic_id = _diagnostic_id(draft.input_binding_sha256, cause.reason, key)
        is_conflict = cause.reason in ("identity_unresolved", "continuity_conflict")
        owner = "conflict_diagnostics" if is_conflict else "evidence_diagnostics"
        expected_refs[key] = SemanticObjectRef(values.identity(owner), "diagnostic", diagnostic_id)
        if not is_conflict:
            expected_evidence.add(diagnostic_id)
            actual = evidence_by_id.get(diagnostic_id)
            if actual is None or (
                actual.reason_code != cause.reason
                or actual.scope_ref != cause.origin
                or frozenset(actual.evidence_refs) != cause.evidence
                or frozenset(actual.affected_refs) != affected[key]
                or actual.measurement != cause.measurement
                or actual.continuity_claim != cause.continuity
                or actual.summary != cause.summary
            ):
                violations["KC-COV-003"].add("evidence_cause_mismatch")
        else:
            expected_conflicts.add(diagnostic_id)
            for claim in cause.claims:
                expected_claims[claim.claim_id] = claim
            if cause.merge is not None:
                expected_merges[key] = cause.merge
            actual_conflict = conflicts_by_id.get(diagnostic_id)
            kind = (
                "possible_duplicate"
                if cause.reason == "identity_unresolved"
                else "timeline_order_conflict"
            )
            if actual_conflict is None or (
                actual_conflict.kind != kind
                or actual_conflict.cause_id != key
                or actual_conflict.scope_ref != cause.origin
                or frozenset(actual_conflict.evidence_refs) != cause.evidence
                or frozenset(actual_conflict.affected_refs) != affected[key]
                or set(actual_conflict.competing_claim_ids)
                != {claim.claim_id for claim in cause.claims}
            ):
                violations["KC-COV-004"].add("conflict_cause_mismatch")
    if set(evidence_by_id) != expected_evidence:
        violations["KC-COV-003"].add("evidence_cause_set_mismatch")
    if set(conflicts_by_id) != expected_conflicts:
        violations["KC-COV-004"].add("conflict_cause_set_mismatch")
    if {claim.claim_id: claim for claim in conflicts.claims} != expected_claims:
        violations["KC-COV-004"].add("original_claim_mismatch")
    if {cause.cause_id: cause for cause in conflicts.merge_causes} != expected_merges:
        violations["KC-COV-004"].add("original_merge_proposal_mismatch")
    return expected_refs, affected


def _check_rows(
    truth: _Truth,
    diagnostics: dict[str, SemanticObjectRef],
    affected: dict[str, frozenset[SemanticObjectRef]],
    violations: dict[str, set[str]],
) -> None:
    ledger, values = truth.values.coverage_ledger, truth.values
    expected = {unit.reference: unit for unit in truth.units.values()}
    windows = {window.window_id: window.source_window_ref for window in ledger.windows}
    seeds = {seed.seed_id: seed for seed in ledger.taint_seeds}
    graph_objects = {(node.node_type, node.node_id) for node in values.narrative_graph.nodes}
    if (
        ledger.input_binding_sha256 != truth.draft.input_binding_sha256
        or ledger.draft_sha256 != truth.draft.canonical_hash
        or ledger.coverage_policy_sha256 != truth.policy.canonical_hash
    ):
        violations["KC-COV-003"].add("ledger_input_binding_mismatch")
    for row in ledger.rows:
        reference = (
            windows[row.unit_ref.window_id]
            if isinstance(row.unit_ref, LocalCoverageWindowRef)
            else row.unit_ref
        )
        unit = expected.get(reference)
        if unit is None:
            continue  # universe checks record missing/foreign units separately
        reasons = {truth.causes[key].reason for key in unit.causes}
        rule = "KC-COV-004" if "continuity_conflict" in reasons else "KC-COV-003"
        status = truth.status(unit)
        disposition = "unassigned" if status == "unresolved" else unit.disposition
        if row.resolution_status != status or row.disposition != disposition:
            violations[rule].add("direct_resolution_mismatch")
        if set(row.assignment_refs) != unit.assignments:
            violations[rule].add("assignment_mismatch")
        if frozenset(row.evidence_refs) != unit.evidence:
            violations[rule].add("row_evidence_mismatch")
        for ref in row.assignment_refs:
            if ref.member_ref.artifact_type == "narrative_graph" and (
                ref.member_ref != values.identity("narrative_graph")
                or (ref.object_type, ref.object_id) not in graph_objects
            ):
                violations[rule].add("assignment_object_missing")
        if set(row.diagnostic_refs) != {diagnostics[key] for key in unit.causes}:
            violations[rule].add("row_diagnostic_mismatch")
        if not unit.causes:
            if row.taint_seed_id is not None:
                violations[rule].add("unexpected_taint_seed")
            continue
        seed = seeds.get(row.taint_seed_id or "")
        if seed is None:
            violations[rule].add("missing_taint_seed")
            continue
        roots = {unit.reference}.union(*(affected[key] for key in unit.causes))
        actual_roots = set(seed.root_refs) | {windows[key] for key in seed.root_window_ids}
        if roots != actual_roots or set(seed.reason_codes) != reasons:
            violations[rule].add("seed_cause_roots_mismatch")
        frontier: set[SemanticObjectRef] = (
            roots if reasons & {"identity_unresolved", "continuity_missing_context"} else set()
        )
        actual_frontier = set(seed.frontier_refs) | {
            windows[key] for key in seed.frontier_window_ids
        }
        if frontier != actual_frontier:
            violations[rule].add("seed_frontier_mismatch")


def verify_coverage_members(
    inputs: CommittedSemanticInputs,
    raw_draft: bytes,
    *,
    members: tuple[ArtifactMember, ...],
    draft_policy: Stage1DraftPolicy,
    coverage_policy: Stage1CoveragePolicy,
) -> tuple[Stage1Check, ...]:
    """Return exactly seven independent checks; malformed shape raises ValueError."""
    if (
        type(inputs) is not CommittedSemanticInputs
        or type(coverage_policy) is not Stage1CoveragePolicy
    ):  # noqa: E721
        raise ValueError("coverage verification requires exact inputs and explicit policy")
    draft = decode_stage1_draft(raw_draft, inputs=inputs, policy=draft_policy)
    values = decode_coverage_members(members, scope=inputs.source_manifest.reference.scope)
    truth = _Truth(inputs, draft, values, coverage_policy)
    truth.observations()
    truth.unknowns(draft_policy)
    truth.assignments()
    violations: dict[str, set[str]] = {rule: set() for rule in _RULES}
    _check_universes(truth, violations)
    diagnostics, affected = _check_diagnostics(truth, raw_draft, violations)
    _check_rows(truth, diagnostics, affected, violations)
    # No exclusion strategy is implemented. A real scan, not a default result.
    for row in values.coverage_ledger.rows:
        if row.disposition not in ("narrative", "supporting", "unassigned"):
            violations["KC-COV-005"].add("unsupported_exclusion")
            violations["KC-EXCLUDE-001"].add("unsupported_exclusion")
    if any(unit.causes for unit in truth.units.values()):
        violations["KC-GATE-001"].add("actual_direct_taint")
    if values.coverage_ledger.taint_seeds or any(
        row.resolution_status != "resolved" for row in values.coverage_ledger.rows
    ):
        violations["KC-GATE-001"].add("declared_taint")
    return tuple(
        Stage1Check(rule, "fail" if violations[rule] else "pass", tuple(sorted(violations[rule])))
        for rule in _RULES
    )
